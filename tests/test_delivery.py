"""At-least-once delivery and load-aware balancing: the broker redelivers work a worker loses, fails or
sits on, holds work for a language with no worker, mirrors in-flight work to the standby brokers, and
workers ignore what they have already handled."""

import json
import queue
import socket
import threading
import time
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import broker as broker_module
import translator_worker
from broker import Broker, WorkerConnection
from common import config
from common.protocol import decode_stream, encode, recv_one
from test_broker_server import FakeSocket, Session, leader_broker, wait_for, worker
from translator_worker import TranslatorWorker


def chunk(n=1, **extra):
    return {"type": "transcript_chunk", "chunk_id": f"c{n}", "text": f"text {n}", "source_lang": "en",
            "targets": ["th"], **extra}


def frames(sock):
    return [f for f in sock.frames if f["type"] == "transcript_chunk"]


def two_workers(broker, lang="th"):
    a, b = FakeSocket(), FakeSocket()
    wa, wb = worker(lang, 1, a), worker(lang, 2, b)
    broker.registry.add(wa)
    broker.registry.add(wb)
    return (wa, a), (wb, b)


class LoadAwareBalancingTests(unittest.TestCase):
    def test_equally_idle_workers_share_work_in_turn(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        for n in range(4):
            broker.route_chunk(chunk(n))
        self.assertEqual([f["chunk_id"] for f in frames(a)], ["c0", "c2"])
        self.assertEqual([f["chunk_id"] for f in frames(b)], ["c1", "c3"])

    def test_the_worker_with_fewer_outstanding_jobs_gets_the_next_one(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        for n in range(3):
            wa.assigned[f"old{n}"] = {}        # a is busy with three jobs
        for n in range(3):
            broker.route_chunk(chunk(n))
        self.assertEqual(len(frames(a)), 0)
        self.assertEqual(len(frames(b)), 3)

    def test_a_worker_that_never_finishes_stops_being_chosen(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        for n in range(10):
            broker.route_chunk(chunk(n))
            for sent in list(frames(b))[-1:]:          # b answers at once, a never does
                broker._on_translated(wb, {"type": "translated_chunk", "chunk_id": sent["chunk_id"], "lang": "th", "text": "x"})
        self.assertLessEqual(len(frames(a)), 2)
        self.assertGreaterEqual(len(frames(b)), 8)

    def test_finishing_a_job_frees_the_workers_capacity_again(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        self.assertEqual(len(wa.assigned), 1)
        broker._on_translated(wa, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
        self.assertEqual(wa.assigned, {})
        self.assertNotIn(("c1", "th"), broker.pending)

    def test_choose_can_leave_out_named_workers(self):
        broker = leader_broker()
        (wa, _), (wb, _) = two_workers(broker)
        self.assertEqual(broker.registry.choose("th", {"th-1"}).worker_id, "th-2")
        self.assertIsNone(broker.registry.choose("th", {"th-1", "th-2"}))
        self.assertIsNone(broker.registry.choose("km"))

    def test_the_busiest_worker_is_never_chosen_over_an_idle_one_whatever_the_rotation(self):
        broker = leader_broker()
        (wa, _), (wb, _) = two_workers(broker)
        wb.assigned.update({f"j{n}": {} for n in range(5)})
        self.assertEqual({broker.registry.choose("th").worker_id for _ in range(6)}, {"th-1"} | set())

    def test_telemetry_reports_each_workers_queue_depth(self):
        broker = leader_broker()
        (wa, _), (wb, _) = two_workers(broker)
        broker.route_chunk(chunk(1))
        queues = {m["worker_id"]: m["queue"] for m in broker.telemetry()["members"]}
        self.assertEqual(sum(queues.values()), 1)


class RedeliveryAfterWorkerLossTests(unittest.TestCase):
    def test_a_lost_workers_job_goes_to_another_worker_of_the_same_language(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        self.assertEqual(len(frames(a)), 1)
        removed = broker.registry.remove("th-1", close=False)
        broker._on_worker_gone(removed)
        self.assertEqual([f["chunk_id"] for f in frames(b)], ["c1"])
        self.assertEqual(broker.pending[("c1", "th")]["worker_id"], "th-2")
        self.assertEqual(broker.pending[("c1", "th")]["attempts"], 2)
        self.assertIn("Redelivered", broker.jobs[("c1", "th")]["detail"])
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "queued")

    def test_a_job_is_not_shown_as_interrupted_when_it_was_moved_to_a_live_worker(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._on_worker_gone(broker.registry.remove("th-1", close=False))
        row = next(j for j in broker.telemetry()["jobs"] if j["chunk_id"] == "c1")
        self.assertEqual(row["status"], "queued")

    def test_over_real_connections_closing_a_worker_hands_its_chunk_to_the_other(self):
        broker = leader_broker()
        s1, s2 = Session(broker), Session(broker)
        for session, wid, port in ((s1, "th-1", 8101), (s2, "th-2", 8102)):
            self.assertTrue(session.request({"type": "register", "role": "worker", "worker_id": wid, "lang": "th",
                                             "host": "h", "port": port})["accepted"])
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        stt.send(chunk(1))
        got = s1.recv()                                    # round robin: th-1 first
        self.assertEqual(got["chunk_id"], "c1")
        s1.close()                                         # th-1 dies without answering
        self.assertEqual(s2.recv()["chunk_id"], "c1")
        s2.close()
        stt.close()

    def test_with_no_other_worker_the_job_waits_then_goes_to_the_next_worker_that_connects(self):
        broker = leader_broker()
        s1 = Session(broker)
        s1.request({"type": "register", "role": "worker", "worker_id": "th-1", "lang": "th", "host": "h", "port": 1})
        broker.route_chunk(chunk(1))
        self.assertEqual(s1.recv()["chunk_id"], "c1")
        s1.close()
        self.assertTrue(wait_for(lambda: broker.registry.snapshot("th") == []))
        self.assertTrue(wait_for(lambda: len(broker.waiting["th"]) == 1))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "unavailable")
        self.assertIn("Waiting for a worker", broker.jobs[("c1", "th")]["detail"])
        s2 = Session(broker)
        s2.request({"type": "register", "role": "worker", "worker_id": "th-2", "lang": "th", "host": "h", "port": 2})
        self.assertEqual(s2.recv()["chunk_id"], "c1")
        self.assertEqual(len(broker.waiting["th"]), 0)
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "queued")
        s2.close()

    def test_a_job_is_given_up_after_the_delivery_attempt_limit(self):
        broker = leader_broker()
        workers = [(worker("th", n, FakeSocket())) for n in (1, 2, 3, 4)]
        for w in workers:
            broker.registry.add(w)
        with mock.patch.object(broker_module, "MAX_ATTEMPTS", 3):
            broker.route_chunk(chunk(1))
            for n in (1, 2, 3):
                holder = next(w for w in workers if "c1" in w.assigned)
                broker._on_worker_gone(broker.registry.remove(holder.worker_id, close=False))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")
        self.assertIn("retry limit", broker.jobs[("c1", "th")]["detail"])
        self.assertNotIn(("c1", "th"), broker.pending)

    def test_losing_a_worker_with_nothing_in_flight_changes_nothing(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker._on_worker_gone(broker.registry.remove("th-1", close=False))
        self.assertEqual((broker.pending, broker.jobs), ({}, {}))

    def test_a_send_failure_during_routing_redelivers_that_workers_other_jobs_too(self):
        broker = leader_broker()
        dead, alive = FakeSocket(), FakeSocket()
        wd, wl = worker("th", 1, dead), worker("th", 2, alive)
        broker.registry.add(wd)
        wd.assigned["earlier"] = {"type": "transcript_chunk", "chunk_id": "earlier", "text": "t"}
        broker.pending[("earlier", "th")] = {"chunk": wd.assigned["earlier"], "worker_id": "th-1", "attempts": 1,
                                             "sent_at": time.monotonic(), "excluded": set()}
        broker.registry.add(wl)
        wl.assigned.update({f"busy{n}": {} for n in range(5)})      # so the dead worker is the one chosen first
        dead.fail = True
        broker.route_chunk(chunk(1))
        self.assertEqual({f["chunk_id"] for f in frames(alive)}, {"c1", "earlier"})

    def test_demotion_drops_the_in_flight_bookkeeping(self):
        broker = leader_broker()
        two_workers(broker)
        broker.route_chunk(chunk(1))
        broker.election.leader_id = 2
        broker._leader_changed(2)
        self.assertEqual((broker.pending, list(broker.waiting["th"])), ({}, []))


class WaitingForAWorkerTests(unittest.TestCase):
    def test_a_chunk_for_a_language_with_no_worker_is_kept_and_reported_unavailable(self):
        broker = leader_broker()
        broker.route_chunk(chunk(1))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "unavailable")
        self.assertEqual(len(broker.waiting["th"]), 1)

    def test_waiting_jobs_that_outlive_the_limit_are_given_up_not_delivered(self):
        broker = leader_broker()
        with mock.patch.object(broker_module, "WAITING_MAX_AGE", 0.05):
            broker.route_chunk(chunk(1))
            time.sleep(0.1)
            broker._expire_waiting()
            self.assertEqual(len(broker.waiting["th"]), 0)
            self.assertIn("No worker connected in time", broker.jobs[("c1", "th")]["detail"])
            late = FakeSocket()
            broker.registry.add(worker("th", 1, late))
            broker._flush_waiting("th")
        self.assertEqual(frames(late), [])

    def test_an_expired_job_is_dropped_at_flush_time_too(self):
        broker = leader_broker()
        broker.route_chunk(chunk(1))
        with mock.patch.object(broker_module, "WAITING_MAX_AGE", 0.01):
            time.sleep(0.06)          # more than the Windows clock tick
            late = FakeSocket()
            broker.registry.add(worker("th", 1, late))
            broker._flush_waiting("th")
        self.assertEqual(frames(late), [])

    def test_the_waiting_queue_is_bounded_and_keeps_the_newest(self):
        broker = leader_broker()
        for n in range(broker_module.WAITING_MAX_PER_LANGUAGE + 20):
            broker.route_chunk(chunk(n))
        queued = [e["chunk"]["chunk_id"] for e in broker.waiting["th"]]
        self.assertEqual(len(queued), broker_module.WAITING_MAX_PER_LANGUAGE)
        self.assertEqual(queued[-1], f"c{broker_module.WAITING_MAX_PER_LANGUAGE + 19}")
        self.assertNotIn("c0", queued)

    def test_waiting_jobs_are_delivered_in_arrival_order(self):
        broker = leader_broker()
        for n in range(3):
            broker.route_chunk(chunk(n))
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        broker._flush_waiting("th")
        self.assertEqual([f["chunk_id"] for f in frames(sock)], ["c0", "c1", "c2"])

    def test_other_languages_waiting_queues_are_untouched(self):
        broker = leader_broker()
        broker.route_chunk({**chunk(1), "targets": ["th", "km"]})
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        broker._flush_waiting("th")
        self.assertEqual(len(broker.waiting["km"]), 1)
        self.assertEqual(len(frames(sock)), 1)


class RetryAfterAFailureTests(unittest.TestCase):
    def failed(self, wid, cid="c1", detail="Translation failed; inspect worker logs"):
        return {"type": "chunk_status", "chunk_id": cid, "lang": "th", "worker_id": wid, "status": "failed", "detail": detail}

    def test_a_failure_is_retried_on_another_worker(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._on_status(wa, self.failed("th-1"))
        self.assertEqual([f["chunk_id"] for f in frames(b)], ["c1"])
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "queued")
        self.assertIn("Retried on another worker", broker.jobs[("c1", "th")]["detail"])

    def test_with_only_one_worker_a_failure_is_final(self):
        broker = leader_broker()
        sock = FakeSocket()
        w = worker("th", 1, sock)
        broker.registry.add(w)
        broker.route_chunk(chunk(1))
        broker._on_status(w, self.failed("th-1"))
        self.assertEqual(len(frames(sock)), 1)                    # not sent again to the same worker
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")
        self.assertNotIn(("c1", "th"), broker.pending)
        self.assertEqual(w.assigned, {})

    def test_a_quota_failure_is_not_retried_because_another_worker_shares_the_quota(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._on_status(wa, self.failed("th-1", detail="Gemini daily quota exhausted"))
        self.assertEqual(frames(b), [])
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")

    def test_retries_stop_at_the_attempt_limit(self):
        broker = leader_broker()
        workers = [worker("th", n, FakeSocket()) for n in (1, 2, 3, 4)]
        for w in workers:
            broker.registry.add(w)
        with mock.patch.object(broker_module, "MAX_ATTEMPTS", 2):
            broker.route_chunk(chunk(1))
            first = next(w for w in workers if "c1" in w.assigned)
            broker._on_status(first, self.failed(first.worker_id))
            second = next(w for w in workers if "c1" in w.assigned)
            broker._on_status(second, self.failed(second.worker_id))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")
        self.assertEqual(sum(len(frames(w.sock)) for w in workers), 2)

    def test_a_stale_failure_from_a_worker_that_no_longer_holds_the_job_is_ignored(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._on_status(wb, self.failed("th-2"))               # b was never given c1
        self.assertEqual(frames(b), [])
        self.assertEqual(len(frames(a)), 1)                       # ...and it was not sent to a a second time
        self.assertIn(("c1", "th"), broker.pending)               # a still has it...
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "queued")      # ...and the job is not marked failed

    def test_progress_statuses_update_the_job_without_touching_delivery(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._on_status(wa, {"type": "chunk_status", "chunk_id": "c1", "lang": "th", "worker_id": "th-1", "status": "translating"})
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "translating")
        self.assertIn(("c1", "th"), broker.pending)


class StuckJobTests(unittest.TestCase):
    def test_a_job_a_connected_worker_never_finishes_goes_to_another(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        with mock.patch.object(broker_module, "JOB_TIMEOUT", 0.05):
            time.sleep(0.1)
            broker._redeliver_stuck()
        self.assertEqual([f["chunk_id"] for f in frames(b)], ["c1"])
        self.assertEqual(wa.assigned, {})
        self.assertIn("did not finish in time", broker.jobs[("c1", "th")]["detail"])

    def test_a_job_that_is_not_old_enough_is_left_alone(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker._redeliver_stuck()
        self.assertEqual(frames(b), [])

    def test_with_nobody_else_to_try_a_stuck_job_is_failed(self):
        broker = leader_broker()
        sock = FakeSocket()
        w = worker("th", 1, sock)
        broker.registry.add(w)
        broker.route_chunk(chunk(1))
        with mock.patch.object(broker_module, "JOB_TIMEOUT", 0.05):
            time.sleep(0.1)
            broker._redeliver_stuck()
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")

    def test_the_reaper_redelivers_stuck_jobs_and_expires_waiting_ones(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        with mock.patch.object(broker_module, "JOB_TIMEOUT", 0.05), mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.02):
            thread = threading.Thread(target=broker._worker_reaper, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: len(frames(b)) == 1))
            broker.stop_event.set()
            thread.join(1)


class DuplicateTests(unittest.TestCase):
    def test_a_producer_that_resends_a_chunk_does_not_get_it_translated_twice(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker.route_chunk(chunk(1))
        self.assertEqual(len(frames(a)) + len(frames(b)), 1)

    def test_a_finished_chunk_that_is_resent_is_not_translated_again(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        holder = wa if wa.assigned else wb
        broker._on_translated(holder, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
        broker.route_chunk(chunk(1))
        self.assertEqual(len(frames(a)) + len(frames(b)), 1)

    def test_a_chunk_that_failed_can_be_sent_again(self):
        broker = leader_broker()
        sock = FakeSocket()
        w = worker("th", 1, sock)
        broker.registry.add(w)
        broker.route_chunk(chunk(1))
        broker._on_status(w, {"type": "chunk_status", "chunk_id": "c1", "lang": "th", "worker_id": "th-1", "status": "failed"})
        broker.route_chunk(chunk(1))
        self.assertEqual(len(frames(sock)), 2)

    def test_the_two_stage_send_still_reaches_every_language(self):
        broker = leader_broker()
        socks = {lang: FakeSocket() for lang in ("th", "km", "my")}
        for n, (lang, sock) in enumerate(socks.items(), 1):
            broker.registry.add(worker(lang, n, sock))
        broker.route_chunk({**chunk(1), "source_lang": "my", "targets": ["km", "my"]})
        broker.route_chunk({**chunk(1), "source_lang": "my", "targets": ["th"], "pivot_en": "Hello"})
        self.assertEqual(len(frames(socks["km"])), 1)
        self.assertEqual(len(frames(socks["th"])), 1)
        self.assertEqual(frames(socks["th"])[0]["pivot_en"], "Hello")

    def test_a_repeated_result_is_published_once(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        holder = wa if wa.assigned else wb
        result = {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"}
        broker._on_translated(holder, result)
        broker._on_translated(holder, result)
        published = [f for f in a.frames + b.frames if f["type"] == "translated_chunk"]
        self.assertEqual(len(published), 2)          # one publish = one copy to each of the two th workers, not four

    def test_a_result_for_an_unknown_job_is_still_published(self):
        # e.g. the broker restarted and lost its table: the answer is still wanted
        broker = leader_broker()
        sock = FakeSocket()
        w = worker("th", 1, sock)
        broker.registry.add(w)
        broker._on_translated(w, {"type": "translated_chunk", "chunk_id": "old", "lang": "th", "text": "x"})
        self.assertEqual(len([f for f in sock.frames if f["type"] == "translated_chunk"]), 1)


def follower_and_leader():
    leader = leader_broker(2)
    follower = Broker(1)
    follower.election.leader_id = 2
    return leader, follower


class ReplicationTests(unittest.TestCase):
    def test_a_routed_chunk_is_mirrored_to_the_other_brokers_in_a_batch(self):
        leader, _ = follower_and_leader()
        leader.registry.add(worker("th", 1, FakeSocket()))
        leader.route_chunk(chunk(1, audio_pcm_b64="AAAA"))
        sent = []
        with mock.patch.object(leader, "_peer_request", side_effect=lambda pid, msg: sent.append((pid, msg))):
            self.assertTrue(leader._replicate_flush())
        self.assertEqual([pid for pid, _ in sent], [1])
        batch = sent[0][1]
        self.assertEqual(batch["type"], "replicate_batch")
        self.assertEqual(batch["adds"][0]["langs"], ["th"])
        self.assertNotIn("audio_pcm_b64", batch["adds"][0]["chunk"])          # large audio is not replicated
        self.assertEqual(batch["adds"][0]["chunk"]["text"], "text 1")

    def test_a_job_finished_before_the_flush_is_never_sent_at_all(self):
        leader, _ = follower_and_leader()
        w = worker("th", 1, FakeSocket())
        leader.registry.add(w)
        leader.route_chunk(chunk(1))
        leader._on_translated(w, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
        with mock.patch.object(leader, "_peer_request") as request:
            self.assertFalse(leader._replicate_flush())
        request.assert_not_called()

    def test_finishing_a_job_after_a_flush_tells_the_standby_to_forget_it(self):
        leader, follower = follower_and_leader()
        w = worker("th", 1, FakeSocket())
        leader.registry.add(w)
        leader.route_chunk(chunk(1))
        wire = lambda pid, msg: follower._apply_replication(msg)
        with mock.patch.object(leader, "_peer_request", side_effect=wire):
            leader._replicate_flush()
            self.assertIn(("c1", "th"), follower.replica)
            leader._on_translated(w, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
            leader._replicate_flush()
        self.assertNotIn(("c1", "th"), follower.replica)

    def test_a_single_broker_cluster_replicates_nothing(self):
        with mock.patch.object(config, "BROKERS", [{"id": 1, "host": "127.0.0.1", "port": 1}]):
            solo = leader_broker(1)
        solo.registry.add(worker("th", 1, FakeSocket()))
        solo.route_chunk(chunk(1))
        self.assertFalse(solo._replicate_flush())

    def test_a_standby_applies_a_batch_it_receives_over_a_connection(self):
        _, follower = follower_and_leader()
        session = Session(follower)
        reply = session.request({"type": "replicate_batch", "leader_id": 2,
                                 "adds": [{"chunk": chunk(5), "langs": ["th", "km"]}], "done": []})
        session.close()
        self.assertEqual(reply["type"], "heartbeat_ack")
        self.assertEqual(set(follower.replica), {("c5", "th"), ("c5", "km")})

    def test_the_standby_keeps_a_bounded_recent_window(self):
        _, follower = follower_and_leader()
        with mock.patch.object(broker_module, "REPLICA_MAX_JOBS", 10):
            follower._apply_replication({"adds": [{"chunk": chunk(n), "langs": ["th"]} for n in range(25)], "done": []})
        self.assertEqual(len(follower.replica), 10)
        self.assertIn(("c24", "th"), follower.replica)
        with mock.patch.object(broker_module, "REPLICA_MAX_AGE", 0.05):
            time.sleep(0.1)
            follower._apply_replication({"adds": [{"chunk": chunk(99), "langs": ["th"]}], "done": []})
        self.assertEqual(set(follower.replica), {("c99", "th")})

    def test_malformed_replication_entries_are_ignored(self):
        _, follower = follower_and_leader()
        follower._apply_replication({"adds": [{"chunk": {}, "langs": ["th"]}, {"chunk": chunk(1), "langs": ["zz"]}, {}], "done": [["x", "th"]]})
        self.assertEqual(follower.replica, {})

    def test_a_new_leader_replays_the_work_the_old_one_had_not_finished(self):
        _, follower = follower_and_leader()
        follower._apply_replication({"adds": [{"chunk": chunk(1), "langs": ["th"]}, {"chunk": chunk(2), "langs": ["th"]}], "done": []})
        sock = FakeSocket()
        follower.registry.add(worker("th", 1, sock))
        with mock.patch.object(broker_module, "REPLAY_DELAY", 0.02):
            follower.election.leader_id = 1
            follower._leader_changed(1)
            self.assertTrue(wait_for(lambda: len(frames(sock)) == 2))
        self.assertEqual(follower.replica, {})
        self.assertIn("Recovered after a broker failover", follower.jobs[("c1", "th")]["detail"])

    def test_replayed_work_waits_for_workers_that_have_not_reconnected_yet(self):
        _, follower = follower_and_leader()
        follower._apply_replication({"adds": [{"chunk": chunk(1), "langs": ["th"]}], "done": []})
        with mock.patch.object(broker_module, "REPLAY_DELAY", 0.02):
            follower.election.leader_id = 1
            follower._leader_changed(1)
            self.assertTrue(wait_for(lambda: len(follower.waiting["th"]) == 1))
        sock = FakeSocket()
        follower.registry.add(worker("th", 1, sock))
        follower._flush_waiting("th")
        self.assertEqual([f["chunk_id"] for f in frames(sock)], ["c1"])

    def test_replay_skips_work_this_broker_already_has(self):
        _, follower = follower_and_leader()
        sock = FakeSocket()
        follower.registry.add(worker("th", 1, sock))
        follower.election.leader_id = 1
        follower.route_chunk(chunk(1))
        follower._apply_replication({"adds": [{"chunk": chunk(1), "langs": ["th"]}], "done": []})
        with mock.patch.object(broker_module, "REPLAY_DELAY", 0.0):
            follower._replay_replica()
        self.assertEqual(len(frames(sock)), 1)

    def test_a_broker_that_lost_leadership_again_does_not_replay(self):
        _, follower = follower_and_leader()
        follower._apply_replication({"adds": [{"chunk": chunk(1), "langs": ["th"]}], "done": []})
        sock = FakeSocket()
        follower.registry.add(worker("th", 1, sock))
        follower.election.leader_id = 2          # someone else leads
        with mock.patch.object(broker_module, "REPLAY_DELAY", 0.0):
            follower._replay_replica()
        self.assertEqual(frames(sock), [])

    def test_a_follower_that_is_not_yet_leader_does_not_replay_on_replication(self):
        _, follower = follower_and_leader()
        follower._apply_replication({"adds": [{"chunk": chunk(1), "langs": ["th"]}], "done": []})
        follower._leader_changed(2)
        time.sleep(0.1)
        self.assertEqual(len(follower.replica), 1)

    def test_the_replicator_thread_only_runs_while_leading(self):
        leader, follower = follower_and_leader()
        leader.registry.add(worker("th", 1, FakeSocket()))
        leader.route_chunk(chunk(1))
        wire = lambda pid, msg: follower._apply_replication(msg)
        with mock.patch.object(broker_module, "REPLICATION_INTERVAL", 0.02), mock.patch.object(leader, "_peer_request", side_effect=wire):
            thread = threading.Thread(target=leader._replicator, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: ("c1", "th") in follower.replica))
            leader.stop_event.set()
            thread.join(1)


class FakeBrokerForWorkers:
    """A listening socket that behaves like the leader for a TranslatorWorker (and lets the test drive it)."""

    def __init__(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.conns = {}
        self.received = queue.Queue()
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            first = recv_one(conn)
            self.conns[first["worker_id"]] = conn
            conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": 1}))
            for message in decode_stream(conn):
                self.received.put(message)
        except Exception:
            pass

    def send(self, message, worker_id="th-1"):
        self.conns[worker_id].sendall(encode(message))

    def results(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        found = []
        while time.monotonic() < deadline:
            try:
                message = self.received.get(timeout=0.1)
            except queue.Empty:
                continue
            found.append(message)
            if message["type"] == "translated_chunk":
                break
        return found

    def close(self):
        self.stop.set()
        self.server.close()
        for conn in self.conns.values():
            conn.close()


class WorkerIdempotenceTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBrokerForWorkers()
        self.addCleanup(self.fake.close)
        for patcher in (mock.patch.object(config, "BROKERS", [{"id": 1, "host": "127.0.0.1", "port": self.fake.port}]),
                        mock.patch.object(config, "RECONNECT_DELAY", 0.05), mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.2),
                        mock.patch.object(config, "CONNECT_TIMEOUT", 0.5)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worker = TranslatorWorker("th", "127.0.0.1", 0, "th-1", mock=True, bind_host="127.0.0.1")
        threading.Thread(target=self.worker.serve, daemon=True).start()
        self.addCleanup(self.worker.shutdown)
        self.assertTrue(wait_for(lambda: "th-1" in self.fake.conns))

    def chunk(self, cid="c1"):
        return {"type": "transcript_chunk", "chunk_id": cid, "text": "hello", "source_lang": "en"}

    def test_a_repeated_chunk_is_translated_once_and_its_stored_result_is_sent_again(self):
        calls = []
        real = self.worker.translator.translate

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        with mock.patch.object(self.worker.translator, "translate", side_effect=counting):
            self.fake.send(self.chunk())
            first = [m for m in self.fake.results() if m["type"] == "translated_chunk"][0]
            self.fake.send(self.chunk())
            second = [m for m in self.fake.results() if m["type"] == "translated_chunk"][0]
        self.assertEqual(len(calls), 1)
        self.assertEqual((first["text"], second["text"]), ("[th] hello", "[th] hello"))
        self.assertGreaterEqual(second["timestamp"], first["timestamp"])

    def test_a_duplicate_that_arrives_while_the_first_is_still_being_translated_is_ignored(self):
        release, started = threading.Event(), threading.Event()
        calls = []
        real = self.worker.translator.translate

        def slow(*args, **kwargs):
            calls.append(1)
            started.set()
            release.wait(5)
            return real(*args, **kwargs)
        with mock.patch.object(self.worker.translator, "translate", side_effect=slow):
            self.fake.send(self.chunk())
            self.assertTrue(started.wait(3))
            self.fake.send(self.chunk())
            time.sleep(0.3)
            release.set()
            results = [m for m in self.fake.results() if m["type"] == "translated_chunk"]
            time.sleep(0.3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 1)

    def test_a_failed_chunk_can_be_retried_because_failures_are_not_remembered(self):
        outcomes = iter([RuntimeError("boom"), ("ok", "mock")])

        def flaky(*args, **kwargs):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(self.worker.translator, "translate", side_effect=flaky):
            self.fake.send(self.chunk())
            time.sleep(0.5)
            self.fake.send(self.chunk())
            done = [m for m in self.fake.results() if m["type"] == "translated_chunk"]
        self.assertEqual(done[0]["text"], "ok")

    def test_the_memory_of_finished_chunks_is_bounded(self):
        for n in range(520):
            self.worker._finished[f"c{n}"] = {"type": "translated_chunk"}
            with self.worker._dedupe_lock:
                while len(self.worker._finished) > 500:
                    self.worker._finished.popitem(last=False)
        self.assertEqual(len(self.worker._finished), 500)

    def test_a_chunk_rejected_for_a_full_queue_can_come_back_later(self):
        self.worker._work = queue.Queue(maxsize=1)
        self.worker._work.put(("x", {}))                      # fill it
        fake_broker = mock.Mock()
        self.worker._accept_chunk(fake_broker, self.chunk("c9"))
        self.assertNotIn("c9", self.worker._queued_ids)
        sent = json.loads(fake_broker.sendall.call_args.args[0].decode())
        self.assertEqual((sent["type"], sent["status"]), ("chunk_status", "failed"))


class EndToEndRedeliveryTests(unittest.TestCase):
    """A real broker and two real workers (mock translators): the first worker dies holding a chunk."""

    def setUp(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        for patcher in (mock.patch.object(config, "BROKERS", [{"id": 1, "host": "127.0.0.1", "port": port}]),
                        mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.1), mock.patch.object(config, "HEARTBEAT_TIMEOUT", 1.0),
                        mock.patch.object(config, "RECONNECT_DELAY", 0.05), mock.patch.object(config, "CONNECT_TIMEOUT", 0.5),
                        mock.patch.object(config, "ELECTION_TIMEOUT", 0.2), mock.patch.object(config, "BROKER_BIND_HOST", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.broker = Broker(1)
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()
        self.addCleanup(self.broker.shutdown)
        self.assertTrue(wait_for(lambda: self.broker.is_leader, 5))

    def start_worker(self, name):
        w = TranslatorWorker("th", "127.0.0.1", 0, name, mock=True, bind_host="127.0.0.1")
        threading.Thread(target=w.serve, daemon=True).start()
        self.addCleanup(w.shutdown)
        return w

    def test_the_chunk_a_dying_worker_was_holding_is_translated_by_the_other(self):
        release = threading.Event()
        first = self.start_worker("th-a")
        self.assertTrue(wait_for(lambda: len(self.broker.registry.snapshot("th")) == 1))
        started = threading.Event()

        def hang(*args, **kwargs):
            started.set()
            release.wait(10)
            raise RuntimeError("worker died")
        first.translator.translate = hang                        # holds the chunk and never answers
        listener_worker = self.start_worker("th-b")
        self.assertTrue(wait_for(lambda: len(self.broker.registry.snapshot("th")) == 2))
        listener = socket.create_connection(("127.0.0.1", listener_worker.port), 2)
        listener.settimeout(8)
        self.addCleanup(listener.close)
        self.assertTrue(wait_for(lambda: len(listener_worker.listeners) == 1))

        # a producer sends one chunk; round robin gives it to th-a first
        producer = socket.create_connection(("127.0.0.1", config.BROKERS[0]["port"]), 2)
        self.addCleanup(producer.close)
        producer.sendall(encode({"type": "register", "role": "stt"}))
        recv_one(producer)
        producer.sendall(encode({"type": "transcript_chunk", "chunk_id": "e2e-1", "text": "hello", "source_lang": "en", "targets": ["th"]}))
        self.assertTrue(started.wait(5), "the first worker never received the chunk")

        # th-a dies mid-translation: the broker's connection to it closes, as when a process is killed
        first.shutdown()
        holder = next(w for w in self.broker.registry.snapshot("th") if w.worker_id == "th-a")
        holder.sock.shutdown(socket.SHUT_RDWR)
        # the survivor translates it within milliseconds, so wait for the finished job rather than the in-flight one
        self.assertTrue(wait_for(lambda: self.broker.jobs.get(("e2e-1", "th"), {}).get("status") == "completed"),
                        "the chunk was not redelivered to the surviving worker")
        release.set()
        message = recv_one(listener)
        self.assertEqual((message["type"], message["chunk_id"], message["text"]), ("translated_chunk", "e2e-1", "[th] hello"))
        self.assertEqual(message["worker_id"], "th-b")
        self.assertEqual(self.broker.jobs[("e2e-1", "th")]["status"], "completed")


class SplitBrainHealingTests(unittest.TestCase):
    """Two brokers that both think they lead (it happened live when a name lookup failed during a restart)."""

    def probe_loop(self, broker, seconds=0.2):
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.02):
            thread = threading.Thread(target=broker._leader_monitor, daemon=True)
            thread.start()
            time.sleep(seconds)
            broker.stop_event.set()
            thread.join(1)

    def test_a_leader_that_finds_a_higher_broker_also_leading_runs_an_election(self):
        low = leader_broker(1)
        with mock.patch.object(low, "_peer_request", return_value={"type": "heartbeat_ack", "is_leader": True}) as probe,                 mock.patch.object(low.election, "start") as start:
            self.probe_loop(low)
        self.assertGreaterEqual(start.call_count, 1)
        self.assertEqual(probe.call_args.args[0], 2)

    def test_a_live_higher_broker_that_is_not_leading_yet_also_makes_it_yield(self):
        low = leader_broker(1)
        with mock.patch.object(low, "_peer_request", return_value={"type": "heartbeat_ack", "is_leader": False}),                 mock.patch.object(low.election, "start") as start:
            self.probe_loop(low)
        self.assertGreaterEqual(start.call_count, 1)

    def test_an_unreachable_higher_broker_leaves_the_leader_alone(self):
        low = leader_broker(1)
        with mock.patch.object(low, "_peer_request", return_value=None), mock.patch.object(low.election, "start") as start:
            self.probe_loop(low)
        start.assert_not_called()

    def test_only_higher_brokers_are_probed(self):
        with mock.patch.object(config, "BROKERS", [{"id": n, "host": "127.0.0.1", "port": 7000 + n} for n in (1, 2, 3, 4)]):
            middle = leader_broker(3)
            probed = []
            with mock.patch.object(middle, "_peer_request", side_effect=lambda pid, msg: probed.append(pid)):
                middle._yield_to_a_higher_broker()
        self.assertEqual(probed, [4])

    def test_two_real_brokers_that_both_lead_settle_on_the_higher_one(self):
        a, b = leader_broker(1), leader_broker(2)       # a split brain: each believes it leads
        for me, other in ((a, b), (b, a)):
            def request(peer_id, message, me=me, other=other):
                if message["type"] == "heartbeat":
                    return {"type": "heartbeat_ack", "is_leader": other.is_leader, "leader_id": other.election.leader_id}
                if message["type"] == "election":
                    threading.Thread(target=other.election.receive_election, args=(message["candidate_id"],), daemon=True).start()
                    return {"type": "election_ok"}
                if message["type"] == "leader_announce":
                    other.election.receive_announcement(message["leader_id"])
                    return {"type": "heartbeat_ack"}
            me._peer_request = request
            me.election.request = request
            me.election.announce = lambda leader_id, me=me, other=other: other.election.receive_announcement(leader_id)
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.02), mock.patch.object(config, "ELECTION_TIMEOUT", 0.3):
            thread = threading.Thread(target=a._leader_monitor, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: a.election.leader_id == 2 and b.election.leader_id == 2, 5),
                            (a.election.leader_id, b.election.leader_id))
            a.stop_event.set()
            thread.join(1)
        self.assertFalse(a.is_leader)
        self.assertTrue(b.is_leader)


class DemotedBrokerDropsProducersTests(unittest.TestCase):
    def test_a_producer_attached_to_a_broker_that_stops_leading_is_disconnected(self):
        broker = leader_broker(1)
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        self.assertTrue(wait_for(lambda: len(broker._clients) == 1))
        broker.election.leader_id = 2
        broker._leader_changed(2)
        self.assertTrue(wait_for(lambda: len(broker._clients) == 0))
        with self.assertRaises((ConnectionError, OSError)):
            stt.recv()
        stt.close()

    def test_a_chunk_that_arrives_after_demotion_is_not_routed_and_the_producer_is_dropped(self):
        broker = leader_broker(1)
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        broker.election.leader_id = 2            # demoted without the producer being told (a race)
        stt.send(chunk(1))
        self.assertTrue(stt.close())
        self.assertEqual(frames(sock), [])

    def test_a_leader_keeps_its_producers(self):
        broker = leader_broker(2)
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        self.assertTrue(wait_for(lambda: len(broker._clients) == 1))      # the broker lists it just after replying
        broker._leader_changed(2)
        self.assertEqual(len(broker._clients), 1)
        stt.close()


if __name__ == "__main__":
    unittest.main()
