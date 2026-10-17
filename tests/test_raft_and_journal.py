"""Raft-style election (POLYGLOT_ELECTION=raft) and the on-disk job journal (POLYGLOT_JOB_JOURNAL)."""

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import broker as broker_module
import job_journal
from common import config
from common.protocol import encode, recv_one
from common.raft import RaftElection
from test_broker_server import FakeSocket, leader_broker, wait_for, worker

ROOT = Path(__file__).resolve().parents[1]


class Network:
    """Brokers talking through function calls; links can be cut to make partitions."""

    def __init__(self, ids, timeout=0.3, directory=None):
        self.ids = list(ids)
        self.cut: set[frozenset] = set()
        self.down: set[int] = set()
        self.changes: dict[int, list] = {i: [] for i in self.ids}
        self.nodes = {i: self.make(i, timeout, directory) for i in self.ids}

    def make(self, node_id, timeout=0.3, directory=None):
        state = os.path.join(directory, f"raft-{node_id}.json") if directory else None
        return RaftElection(node_id, self.ids, self.request_from(node_id), self.changes[node_id].append, timeout, state)

    def request_from(self, source):
        def request(target, message):
            if target in self.down or source in self.down or frozenset((source, target)) in self.cut:
                return None
            node = self.nodes[target]
            if message["type"] == "vote_request":
                return node.handle_vote_request(message)
            if message["type"] == "leader_heartbeat":
                return node.handle_heartbeat(message)
            return None
        return request

    def partition(self, *groups):
        self.cut = {frozenset((a, b)) for g in groups for h in groups if g is not h for a in g for b in h}

    def heal(self):
        self.cut = set()

    def leaders(self):
        return sorted(i for i, n in self.nodes.items() if i not in self.down and n.role == "leader")


class ElectionTests(unittest.TestCase):
    def test_a_candidate_with_a_majority_becomes_leader_and_the_others_follow(self):
        net = Network([1, 2, 3])
        self.assertEqual(net.nodes[1].start(), 1)
        self.assertEqual(net.leaders(), [1])
        self.assertEqual({net.nodes[i].leader_id for i in (2, 3)}, {1})          # told by the first heartbeat
        self.assertEqual(net.changes[1][-1], 1)
        self.assertEqual(net.nodes[2].term, net.nodes[1].term)

    def test_each_broker_votes_once_per_term(self):
        net = Network([1, 2, 3])
        follower = net.nodes[2]
        self.assertTrue(follower.handle_vote_request({"term": 5, "candidate_id": 1})["granted"])
        self.assertFalse(follower.handle_vote_request({"term": 5, "candidate_id": 3})["granted"])
        self.assertTrue(follower.handle_vote_request({"term": 5, "candidate_id": 1})["granted"])   # asking again is fine
        self.assertTrue(follower.handle_vote_request({"term": 6, "candidate_id": 3})["granted"])   # a new term, a new vote

    def test_a_vote_for_an_old_term_is_refused(self):
        net = Network([1, 2, 3])
        net.nodes[2].handle_heartbeat({"term": 9, "leader_id": 3})
        reply = net.nodes[2].handle_vote_request({"term": 4, "candidate_id": 1})
        self.assertEqual((reply["granted"], reply["term"]), (False, 9))

    def test_a_broker_cut_off_in_a_minority_is_never_elected(self):
        net = Network([1, 2, 3])
        net.partition({3}, {1, 2})
        for _ in range(5):
            net.nodes[3].start()
        self.assertNotIn(3, net.leaders())
        self.assertEqual(net.nodes[1].start(), 1)                          # the majority side elects normally
        self.assertEqual(net.leaders(), [1])

    def test_a_leader_that_loses_its_majority_steps_down(self):
        net = Network([1, 2, 3], timeout=0.05)
        net.nodes[1].start()
        net.partition({1}, {2, 3})
        net.nodes[1].leader_tick()
        time.sleep(0.08)
        net.nodes[1].leader_tick()
        self.assertEqual(net.nodes[1].role, "follower")
        self.assertIsNone(net.nodes[1].leader_id)
        self.assertIsNone(net.changes[1][-1])                              # the broker is told: no leader now

    def test_after_a_partition_heals_the_old_leader_follows_the_newer_term(self):
        net = Network([1, 2, 3], timeout=0.05)
        net.nodes[1].start()
        net.partition({1}, {2, 3})
        net.nodes[2].start()                                               # the majority side elects 2 in a newer term
        self.assertIn(2, net.leaders())
        net.heal()
        net.nodes[1].leader_tick()                                         # 1 learns of the newer term and gives way
        net.nodes[2].leader_tick()
        self.assertEqual(net.leaders(), [2])
        self.assertEqual(net.nodes[1].leader_id, 2)

    def test_a_candidate_that_hears_of_a_newer_term_catches_up_and_does_not_lead(self):
        net = Network([1, 2, 3])
        net.nodes[2].handle_heartbeat({"term": 9, "leader_id": 3})
        net.nodes[3].term = 9
        net.down = {3}                                   # 3 is unreachable; only 2 answers, with term 9
        net.nodes[1].start()                             # stands in term 1, far behind
        self.assertNotEqual(net.nodes[1].role, "leader")
        self.assertEqual(net.nodes[1].term, 9)           # caught up, so its next attempt can actually win

    def test_a_heartbeat_from_an_older_term_is_refused(self):
        net = Network([1, 2, 3])
        net.nodes[2].handle_heartbeat({"term": 7, "leader_id": 3})
        reply = net.nodes[2].handle_heartbeat({"term": 6, "leader_id": 1})
        self.assertFalse(reply["success"])
        self.assertEqual(net.nodes[2].leader_id, 3)

    def test_a_follower_stands_only_after_its_timeout_and_a_heartbeat_resets_it(self):
        net = Network([1, 2, 3], timeout=0.05)
        follower = net.nodes[2]
        follower._last_heard = time.monotonic()
        self.assertFalse(follower.heartbeat_expired())
        time.sleep(0.12)
        self.assertTrue(follower.heartbeat_expired())
        follower.handle_heartbeat({"term": 1, "leader_id": 1})
        self.assertFalse(follower.heartbeat_expired())

    def test_election_timeouts_are_randomised_so_brokers_rarely_stand_together(self):
        node = Network([1, 2, 3], timeout=1.0).nodes[1]
        timeouts = [node._new_timeout() for _ in range(30)]
        self.assertTrue(all(1.0 <= t <= 2.0 for t in timeouts))
        self.assertGreater(len(set(round(t, 6) for t in timeouts)), 20)

    def test_a_split_vote_elects_nobody_and_a_later_election_succeeds(self):
        net = Network([1, 2, 3, 4])
        for node in net.nodes.values():
            node.term = 3
        # Term 4: broker 2 is already a candidate (it voted for itself) and broker 4 voted for it; broker 3
        # voted for 1. When 1 stands in term 4 it collects only its own vote and 3's: 2 of 4, not a majority.
        net.nodes[2].term, net.nodes[2].voted_for = 4, 2
        net.nodes[4].handle_vote_request({"term": 4, "candidate_id": 2})
        net.nodes[3].handle_vote_request({"term": 4, "candidate_id": 1})
        self.assertNotEqual(net.nodes[1].start(), 1)
        self.assertEqual(net.leaders(), [])
        self.assertEqual(net.nodes[2].start(), 2)                          # term 5: nobody has voted yet
        self.assertEqual(net.leaders(), [2])

    def test_the_vote_survives_a_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        net = Network([1, 2, 3], directory=directory.name)
        net.nodes[2].handle_vote_request({"term": 8, "candidate_id": 1})
        net.nodes[2] = net.make(2, directory=directory.name)              # broker 2 restarts
        self.assertEqual(net.nodes[2].term, 8)
        self.assertFalse(net.nodes[2].handle_vote_request({"term": 8, "candidate_id": 3})["granted"])

    def test_a_damaged_state_file_starts_fresh(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with open(os.path.join(directory.name, "raft-1.json"), "w") as handle:
            handle.write("{broken")
        node = Network([1, 2, 3], directory=directory.name).nodes[1]
        self.assertEqual((node.term, node.voted_for), (0, None))

    def test_never_two_leaders_in_one_term_whatever_happens(self):
        rng = random.Random(7)
        net = Network([1, 2, 3, 4, 5], timeout=0.0)
        leaders_by_term: dict[int, set] = {}
        for _ in range(400):
            action = rng.random()
            if action < 0.2:
                ids = list(net.ids)
                rng.shuffle(ids)
                cut = rng.randint(1, 4)
                net.partition(set(ids[:cut]), set(ids[cut:]))
            elif action < 0.3:
                net.heal()
            elif action < 0.7:
                net.nodes[rng.choice(net.ids)].start()
            else:
                net.nodes[rng.choice(net.ids)].leader_tick()
            for node_id, node in net.nodes.items():
                if node.role == "leader":
                    leaders_by_term.setdefault(node.term, set()).add(node_id)
        self.assertTrue(leaders_by_term, "no leader was ever elected")
        self.assertTrue(all(len(ids) == 1 for ids in leaders_by_term.values()), leaders_by_term)

    def test_bully_messages_are_ignored_in_raft_mode(self):
        node = Network([1, 2, 3]).nodes[1]
        node.receive_election(3)
        node.receive_announcement(3)
        self.assertIsNone(node.leader_id)


class RaftBrokerTests(unittest.TestCase):
    def raft_broker(self, broker_id=1):
        with mock.patch.object(config, "ELECTION_MODE", "raft"):
            return broker_module.Broker(broker_id)

    def test_the_mode_is_chosen_from_the_configuration(self):
        self.assertTrue(self.raft_broker().raft)
        self.assertFalse(leader_broker().raft)

    def test_replication_from_an_older_term_is_ignored(self):
        standby = self.raft_broker(1)
        standby.election.term = 5
        chunk = {"type": "transcript_chunk", "chunk_id": "c1", "text": "x"}
        sock_a, sock_b = socket.socketpair()
        self.addCleanup(sock_b.close)
        with self.assertLogs("polyglot.broker", level="WARNING"):
            sock_b.sendall(encode({"type": "replicate_batch", "leader_id": 2, "term": 4, "adds": [{"chunk": chunk, "langs": ["th"]}], "done": []}))
            standby._handle_connection(sock_a, ("127.0.0.1", 1))
        self.assertEqual(len(standby.replica), 0)
        sock_a, sock_b = socket.socketpair()
        self.addCleanup(sock_b.close)
        sock_b.sendall(encode({"type": "replicate_batch", "leader_id": 2, "term": 5, "adds": [{"chunk": chunk, "langs": ["th"]}], "done": []}))
        standby._handle_connection(sock_a, ("127.0.0.1", 1))
        self.assertEqual(len(standby.replica), 1)

    def test_vote_and_heartbeat_messages_are_answered(self):
        broker = self.raft_broker(2)
        for message, expected in (({"type": "vote_request", "term": 3, "candidate_id": 1}, "vote_reply"),
                                  ({"type": "leader_heartbeat", "term": 3, "leader_id": 1}, "heartbeat_ack")):
            sock_a, sock_b = socket.socketpair()
            self.addCleanup(sock_b.close)
            sock_b.sendall(encode(message))
            threading.Thread(target=broker._handle_connection, args=(sock_a, ("127.0.0.1", 1)), daemon=True).start()
            sock_b.settimeout(2)
            self.assertEqual(recv_one(sock_b)["type"], expected)
        self.assertEqual(broker.election.leader_id, 1)

    def test_losing_leadership_without_a_successor_drops_the_brokers_work(self):
        broker = leader_broker(1)
        broker.registry.add(worker("th", 1, FakeSocket()))
        broker.pending[("c1", "th")] = {"chunk": {}, "worker_id": "th-1", "attempts": 1, "excluded": [], "sent_at": 0}
        broker.election.leader_id = None
        with self.assertLogs("polyglot.broker", level="INFO") as logs:
            broker._leader_changed(None)
        self.assertEqual(broker.pending, {})
        self.assertTrue(any("No leader at the moment" in line for line in logs.output))


class RealRaftClusterTests(unittest.TestCase):
    """Three real broker processes in Raft mode: one leader, failover, and no leader without a majority."""

    PORTS = (17821, 17822, 17823)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = {**os.environ, "POLYGLOT_ELECTION": "raft", "POLYGLOT_BROKER_COUNT": "3",
                    "POLYGLOT_HEARTBEAT_INTERVAL": "0.2", "POLYGLOT_HEARTBEAT_TIMEOUT": "0.8",
                    "POLYGLOT_ELECTION_TIMEOUT": "0.2", "POLYGLOT_CONNECT_TIMEOUT": "0.3",
                    "POLYGLOT_RAFT_STATE": os.path.join(self.directory.name, "raft-{id}.json"),
                    **{f"POLYGLOT_BROKER_{n}_HOST": "127.0.0.1" for n in (1, 2, 3)},
                    **{f"POLYGLOT_BROKER_{n}_PORT": str(port) for n, port in zip((1, 2, 3), self.PORTS)}}
        self.processes = {n: self.launch(n) for n in (1, 2, 3)}
        self.addCleanup(self.stop_all)

    def launch(self, n):
        return subprocess.Popen([sys.executable, "broker.py", "--id", str(n)], cwd=ROOT, env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self, n):
        process = self.processes[n]
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def stop_all(self):
        for n, process in self.processes.items():
            if process.poll() is None:
                self.stop(n)

    def leaders(self):
        found = []
        for n, port in zip((1, 2, 3), self.PORTS):
            if self.processes[n].poll() is not None:
                continue
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5) as sock:
                    sock.settimeout(0.5)
                    sock.sendall(encode({"type": "heartbeat", "sender_id": 0, "role": "broker"}))
                    if recv_one(sock).get("is_leader"):
                        found.append(n)
            except OSError:
                pass
        return found

    def test_one_leader_failover_and_no_leader_without_a_majority(self):
        self.assertTrue(wait_for(lambda: len(self.leaders()) == 1, 15), "no single leader emerged")
        first = self.leaders()[0]
        self.stop(first)
        self.assertTrue(wait_for(lambda: len(self.leaders()) == 1 and self.leaders()[0] != first, 15), "no failover")
        second = self.leaders()[0]
        self.stop(second)                                    # one broker left: no majority, so no leader
        survivor = ({1, 2, 3} - {first, second}).pop()
        self.assertTrue(wait_for(lambda: self.leaders() == [], 10))
        time.sleep(2)
        self.assertEqual(self.leaders(), [], "a lone broker must not lead in Raft mode")
        self.processes[first] = self.launch(first)           # a majority again
        self.assertTrue(wait_for(lambda: len(self.leaders()) == 1, 15), "no leader after the majority returned")
        self.assertIn(self.leaders()[0], (first, survivor))


class JournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "jobs-1.db")

    def journal(self):
        journal = job_journal.JobJournal(self.path)
        self.addCleanup(journal.close)
        return journal

    def test_jobs_are_kept_until_removed_and_survive_reopening(self):
        journal = self.journal()
        journal.put({"chunk_id": "c1", "text": "hello"}, ["th", "my"])
        journal.remove([("c1", "my")])
        journal.close()
        again = self.journal()
        self.assertEqual([(c, l, chunk["text"]) for c, l, chunk, _ in again.load()], [("c1", "th", "hello")])

    def test_old_jobs_are_not_replayed_and_are_deleted(self):
        journal = self.journal()
        journal.put({"chunk_id": "old"}, ["th"], at=time.time() - 10_000)
        journal.put({"chunk_id": "new"}, ["th"])
        self.assertEqual([c for c, _, _, _ in journal.load()], ["new"])
        self.assertEqual(journal.count(), 1)

    def test_adding_a_job_again_keeps_its_original_time(self):
        journal = self.journal()
        journal.put({"chunk_id": "c1"}, ["th"], at=1000.0 + time.time() - 1000)
        first = journal.load()[0][3]
        journal.put({"chunk_id": "c1"}, ["th"])
        self.assertEqual(journal.load()[0][3], first)

    def test_the_path_comes_from_the_environment_with_the_broker_id(self):
        template = self.path.replace("jobs-1", "jobs-{id}")
        with mock.patch.dict(os.environ, {"POLYGLOT_JOB_JOURNAL": template}):
            journal = job_journal.JobJournal.from_env(3)
        self.addCleanup(journal.close)
        self.assertTrue(journal.path.endswith("jobs-3.db"))
        with mock.patch.dict(os.environ, {"POLYGLOT_JOB_JOURNAL": ""}):
            self.assertIsNone(job_journal.JobJournal.from_env(1))

    def test_an_unusable_location_turns_the_journal_off(self):
        blocker = self.path + ".file"
        open(blocker, "w").close()
        with mock.patch.dict(os.environ, {"POLYGLOT_JOB_JOURNAL": os.path.join(blocker, "x.db")}), \
                self.assertLogs("polyglot.journal", level="WARNING"):
            self.assertIsNone(job_journal.JobJournal.from_env(1))


class JournalBrokerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_JOB_JOURNAL": os.path.join(directory.name, "jobs-{id}.db")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def broker(self, broker_id=1, lead=True):
        broker = broker_module.Broker(broker_id)
        if lead:
            broker.election.leader_id = broker_id
        self.addCleanup(broker.journal.close)
        return broker

    def chunk(self, n=1):
        return {"type": "transcript_chunk", "chunk_id": f"c{n}", "text": f"text {n}", "targets": ["th"]}

    def test_a_routed_job_is_on_disk_until_it_is_finished(self):
        broker = self.broker()
        th = worker("th", 1, FakeSocket())
        broker.registry.add(th)
        broker.route_chunk(self.chunk(1))
        self.assertEqual(broker.journal.count(), 1)
        broker._on_translated(th, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
        self.assertEqual(broker.journal.count(), 0)

    def test_a_job_waiting_for_a_worker_survives_every_broker_restarting(self):
        first = self.broker(1)
        first.route_chunk(self.chunk(7))                       # no Thai worker: the job waits
        self.assertEqual(first.journal.count(), 1)
        first.journal.close()
        restarted = self.broker(1, lead=False)                 # the whole relay restarted
        self.assertIn(("c7", "th"), restarted.replica)
        sock = FakeSocket()
        restarted.registry.add(worker("th", 1, sock))
        restarted.election.leader_id = 1
        with mock.patch.object(broker_module, "REPLAY_DELAY", 0):
            restarted._leader_changed(1)
            self.assertTrue(wait_for(lambda: [f["chunk_id"] for f in sock.frames if f["type"] == "transcript_chunk"] == ["c7"]))

    def test_a_standby_keeps_the_leaders_unfinished_work_on_its_own_disk(self):
        standby = self.broker(2, lead=False)
        standby._apply_replication({"adds": [{"chunk": self.chunk(3), "langs": ["th", "my"]}], "done": [["c3", "my"]]})
        self.assertEqual(standby.journal.count(), 1)
        standby._apply_replication({"adds": [], "done": [["c3", "th"]]})
        self.assertEqual(standby.journal.count(), 0)


if __name__ == "__main__":
    unittest.main()
