"""Broker behaviour that needs no subprocess: the registry, the connection
handler (driven over socketpairs), routing, job telemetry, the worker reaper
and leadership changes."""

import json
import socket
import threading
import time
import unittest
from unittest import mock

from broker import Broker, WorkerConnection, WorkerRegistry
from common import config
from common.protocol import encode, recv_one


class FakeSocket:
    def __init__(self, fail=False):
        self.frames, self.closed, self.fail = [], False, fail

    def sendall(self, payload):
        if self.fail:
            raise OSError("broken pipe")
        self.frames.append(json.loads(payload.decode()))

    def close(self):
        self.closed = True


def worker(lang, number=1, sock=None, **kwargs):
    return WorkerConnection(f"{lang}-{number}", lang, "host", 8100 + number, sock or FakeSocket(), **kwargs)


def leader_broker(broker_id=1):
    broker = Broker(broker_id)
    broker.election.leader_id = broker_id
    return broker


class Session:
    """One client connection served by Broker._handle_connection on a thread."""

    def __init__(self, broker):
        self.client, server_side = socket.socketpair()
        self.client.settimeout(3)
        self.thread = threading.Thread(target=broker._handle_connection, args=(server_side, ("test", 0)), daemon=True)
        self.thread.start()

    def send(self, message):
        self.client.sendall(encode(message))

    def recv(self):
        return recv_one(self.client)

    def request(self, message):
        self.send(message)
        return self.recv()

    def close(self):
        self.client.close()
        self.thread.join(timeout=3)
        return not self.thread.is_alive()


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class WorkerRegistryTests(unittest.TestCase):
    def test_choose_cycles_through_a_pool_in_order(self):
        registry = WorkerRegistry()
        for n in (1, 2, 3):
            registry.add(worker("th", n))
        self.assertEqual([registry.choose("th").worker_id for _ in range(5)], ["th-1", "th-2", "th-3", "th-1", "th-2"])

    def test_choose_returns_none_for_empty_or_unknown_pools(self):
        registry = WorkerRegistry()
        self.assertIsNone(registry.choose("th"))
        self.assertIsNone(registry.choose("zz"))

    def test_re_registering_an_id_replaces_the_old_connection(self):
        registry = WorkerRegistry()
        old, new = FakeSocket(), FakeSocket()
        registry.add(worker("th", 1, old))
        registry.add(worker("th", 1, new))
        self.assertEqual(len(registry.snapshot("th")), 1)
        self.assertIs(registry.choose("th").sock, new)
        self.assertFalse(old.closed)  # the replaced connection is left for its own handler to close

    def test_remove_closes_the_socket_and_keeps_the_cursor_in_range(self):
        registry = WorkerRegistry()
        socks = [FakeSocket() for _ in range(3)]
        for n, sock in enumerate(socks, 1):
            registry.add(worker("th", n, sock))
        registry.choose("th")
        registry.choose("th")
        self.assertEqual(registry.remove("th-3").worker_id, "th-3")
        self.assertTrue(socks[2].closed)
        self.assertIn(registry.choose("th").worker_id, ("th-1", "th-2"))

    def test_remove_without_close_and_unknown_id(self):
        registry = WorkerRegistry()
        sock = FakeSocket()
        registry.add(worker("th", 1, sock))
        registry.remove("th-1", close=False)
        self.assertFalse(sock.closed)
        self.assertIsNone(registry.remove("nobody"))

    def test_snapshot_is_a_copy_and_all_lists_every_pool(self):
        registry = WorkerRegistry()
        registry.add(worker("th", 1))
        registry.add(worker("km", 1))
        registry.snapshot("th").clear()
        self.assertEqual(len(registry.snapshot("th")), 1)
        self.assertEqual({w.lang for w in registry.all()}, {"th", "km"})

    def test_clear_empties_everything_and_closes_sockets(self):
        registry = WorkerRegistry()
        socks = [FakeSocket(), FakeSocket()]
        registry.add(worker("th", 1, socks[0]))
        registry.add(worker("km", 1, socks[1]))
        registry.clear()
        self.assertEqual(registry.all(), [])
        self.assertTrue(all(s.closed for s in socks))


class HandshakeTests(unittest.TestCase):
    def test_election_message_is_answered_ok_and_forwarded(self):
        broker = Broker(2)
        with mock.patch.object(broker.election, "receive_election") as received:
            session = Session(broker)
            reply = session.request({"type": "election", "candidate_id": 1})
            self.assertTrue(session.close())
        self.assertEqual(reply["type"], "election_ok")
        self.assertEqual(reply["responder_id"], 2)
        received.assert_called_once_with(1)

    def test_leader_announcement_updates_the_leader_and_is_acknowledged(self):
        broker = Broker(1)
        session = Session(broker)
        reply = session.request({"type": "leader_announce", "leader_id": 2})
        session.close()
        self.assertEqual(reply["type"], "heartbeat_ack")
        self.assertEqual(broker.election.leader_id, 2)
        self.assertFalse(broker.is_leader)

    def test_broker_heartbeat_reports_leadership(self):
        for leader, expected in ((True, True), (False, False)):
            broker = Broker(1)
            broker.election.leader_id = 1 if leader else 2
            session = Session(broker)
            reply = session.request({"type": "heartbeat", "sender_id": 2, "role": "broker"})
            session.close()
            self.assertEqual(reply["type"], "heartbeat_ack")
            self.assertIs(reply["is_leader"], expected)
            self.assertNotIn("jobs", reply)

    def test_dashboard_heartbeat_includes_members_and_jobs(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1, mode="gemini"))
        broker.update_job({"chunk_id": "c1", "lang": "th", "worker_id": "th-1"}, "queued")
        session = Session(broker)
        reply = session.request({"type": "heartbeat", "sender_id": "dashboard", "role": "broker"})
        session.close()
        self.assertEqual(reply["members"][0]["worker_id"], "th-1")
        self.assertEqual(reply["members"][0]["mode"], "gemini")
        self.assertEqual(reply["jobs"][0]["status"], "queued")

    def test_a_follower_redirects_registration_to_the_leader(self):
        broker = Broker(1)
        broker.election.leader_id = 2
        session = Session(broker)
        reply = session.request({"type": "register", "role": "stt"})
        session.close()
        self.assertFalse(reply["accepted"])
        self.assertEqual(reply["leader"]["id"], 2)

    def test_a_follower_with_no_known_leader_redirects_to_nobody(self):
        session = Session(Broker(1))
        reply = session.request({"type": "register", "role": "worker"})
        session.close()
        self.assertFalse(reply["accepted"])
        self.assertIsNone(reply["leader"])

    def test_a_follower_also_redirects_worker_discovery(self):
        broker = Broker(1)
        broker.election.leader_id = 2
        session = Session(broker)
        reply = session.request({"type": "discover_worker", "lang": "th"})
        session.close()
        self.assertEqual(reply["type"], "register_ack")
        self.assertFalse(reply["accepted"])

    def test_first_message_must_be_a_registration(self):
        session = Session(leader_broker())
        reply = session.request({"type": "transcript_chunk", "chunk_id": "x", "text": "hi"})
        self.assertEqual(reply["type"], "error")
        self.assertTrue(session.close())

    def test_unknown_role_is_rejected(self):
        session = Session(leader_broker())
        reply = session.request({"type": "register", "role": "gremlin"})
        self.assertEqual(reply["type"], "error")
        self.assertTrue(session.close())

    def test_unsupported_worker_language_is_dropped_without_registering(self):
        broker = leader_broker()
        session = Session(broker)
        session.send({"type": "register", "role": "worker", "worker_id": "zz-1", "lang": "zz", "host": "h", "port": 1})
        self.assertTrue(session.close())
        self.assertEqual(broker.registry.all(), [])

    def test_a_client_that_disconnects_immediately_does_not_crash_the_handler(self):
        session = Session(leader_broker())
        self.assertTrue(session.close())

    def test_garbage_bytes_end_the_connection_quietly(self):
        session = Session(leader_broker())
        session.client.sendall(b"this is not json\n")
        self.assertTrue(wait_for(lambda: not session.thread.is_alive()))
        session.client.close()


class DiscoveryAndRegistrationTests(unittest.TestCase):
    def register_worker(self, broker, lang="th", worker_id="th-1", port=8100, mode="gemini"):
        session = Session(broker)
        ack = session.request({"type": "register", "role": "worker", "worker_id": worker_id, "lang": lang,
                               "host": "worker-host", "port": port, "mode": mode})
        return session, ack

    def test_worker_registration_is_acknowledged_and_listed(self):
        broker = leader_broker()
        session, ack = self.register_worker(broker)
        self.assertTrue(ack["accepted"])
        self.assertEqual(ack["leader_id"], 1)
        self.assertEqual(broker.registry.snapshot("th")[0].mode, "gemini")
        session.close()

    def test_worker_is_deregistered_when_its_connection_drops(self):
        broker = leader_broker()
        session, _ = self.register_worker(broker)
        session.close()
        self.assertTrue(wait_for(lambda: broker.registry.all() == []))

    def test_discovery_returns_round_robin_endpoints_then_none(self):
        broker = leader_broker()
        s1, _ = self.register_worker(broker, worker_id="th-1", port=8101)
        s2, _ = self.register_worker(broker, worker_id="th-2", port=8102)
        ports = []
        for _ in range(3):
            probe = Session(broker)
            ports.append(probe.request({"type": "discover_worker", "lang": "th"})["port"])
            probe.close()
        self.assertEqual(ports, [8101, 8102, 8101])
        probe = Session(broker)
        reply = probe.request({"type": "discover_worker", "lang": "km"})
        probe.close()
        self.assertEqual(reply["type"], "worker_info")
        self.assertIsNone(reply["worker_id"])
        s1.close()
        s2.close()

    def test_translated_chunk_completes_the_job_and_reaches_listeners(self):
        broker = leader_broker()
        session, _ = self.register_worker(broker)
        broker.update_job({"chunk_id": "c1", "lang": "th", "worker_id": "th-1"}, "queued")
        session.send({"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "สวัสดี"})
        # the broker echoes it back to the pool's connections (the worker fans it out to listeners)
        echoed = session.recv()
        self.assertEqual(echoed["text"], "สวัสดี")
        self.assertTrue(wait_for(lambda: broker.jobs[("c1", "th")]["status"] == "completed"))
        session.close()

    def test_any_message_from_a_worker_counts_as_a_sign_of_life(self):
        broker = leader_broker()
        session, _ = self.register_worker(broker)
        registered = broker.registry.snapshot("th")[0]
        registered.last_seen = time.monotonic() - 100
        session.send({"type": "heartbeat", "sender_id": "th-1", "role": "worker"})
        self.assertTrue(wait_for(lambda: time.monotonic() - registered.last_seen < 5))
        session.close()

    def test_chunk_status_updates_the_job_and_keeps_the_detail(self):
        broker = leader_broker()
        session, _ = self.register_worker(broker)
        session.send({"type": "chunk_status", "chunk_id": "c2", "lang": "th", "worker_id": "th-1",
                      "status": "translating", "detail": "Gemini request in progress"})
        self.assertTrue(wait_for(lambda: ("c2", "th") in broker.jobs))
        self.assertEqual(broker.jobs[("c2", "th")]["status"], "translating")
        self.assertEqual(broker.jobs[("c2", "th")]["detail"], "Gemini request in progress")
        session.close()

    def test_stt_producer_chunks_are_routed_to_workers(self):
        broker = leader_broker()
        worker_session, _ = self.register_worker(broker, lang="th")
        stt = Session(broker)
        ack = stt.request({"type": "register", "role": "stt"})
        self.assertTrue(ack["accepted"])
        stt.send({"type": "transcript_chunk", "chunk_id": "c3", "text": "hello", "source_lang": "en"})
        forwarded = worker_session.recv()
        self.assertEqual((forwarded["chunk_id"], forwarded["text"]), ("c3", "hello"))
        stt.close()
        worker_session.close()

    def test_stt_ignores_message_types_other_than_transcript_chunks(self):
        broker = leader_broker()
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        stt.send({"type": "heartbeat", "role": "stt"})
        self.assertTrue(stt.close())


class RoutingTests(unittest.TestCase):
    def chunk(self, **extra):
        return {"type": "transcript_chunk", "chunk_id": "c1", "text": "hello", **extra}

    def test_source_language_gets_a_passthrough_without_a_worker_call(self):
        broker = leader_broker()
        th = FakeSocket()
        broker.registry.add(worker("th", 1, th))
        broker.route_chunk(self.chunk(source_lang="th"))
        self.assertEqual(th.frames[0]["translation_backend"], "passthrough")
        self.assertEqual(th.frames[0]["type"], "translated_chunk")
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "source")

    def test_jobs_are_queued_for_workers_and_unavailable_for_empty_pools(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1))
        broker.route_chunk(self.chunk(source_lang="en"))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "queued")
        self.assertEqual(broker.jobs[("c1", "km")]["status"], "unavailable")

    def test_a_dead_worker_is_removed_and_the_next_one_gets_the_chunk(self):
        broker = leader_broker()
        dead, alive = FakeSocket(fail=True), FakeSocket()
        broker.registry.add(worker("th", 1, dead))
        broker.registry.add(worker("th", 2, alive))
        broker.route_chunk(self.chunk(source_lang="en", targets=["th"]))
        self.assertEqual(len(alive.frames), 1)
        self.assertEqual([w.worker_id for w in broker.registry.snapshot("th")], ["th-2"])

    def test_chunk_is_unavailable_when_every_worker_in_the_pool_is_dead(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1, FakeSocket(fail=True)))
        broker.route_chunk(self.chunk(source_lang="en", targets=["th"]))
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "unavailable")
        self.assertEqual(broker.registry.snapshot("th"), [])

    def test_empty_targets_list_routes_nothing(self):
        broker = leader_broker()
        th = FakeSocket()
        broker.registry.add(worker("th", 1, th))
        broker.route_chunk(self.chunk(source_lang="en", targets=[]))
        self.assertEqual(th.frames, [])
        self.assertEqual(broker.jobs, {})

    def test_job_table_is_capped(self):
        broker = leader_broker()
        for i in range(1900):
            broker.update_job({"chunk_id": f"c{i}", "lang": "th"}, "queued")
        self.assertEqual(len(broker.jobs), 1800)
        self.assertNotIn(("c0", "th"), broker.jobs)
        self.assertIn(("c1899", "th"), broker.jobs)

    def test_publish_drops_a_worker_whose_socket_failed(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1, FakeSocket(fail=True)))
        broker._publish_translation({"type": "translated_chunk", "lang": "th", "text": "x"})
        self.assertEqual(broker.registry.snapshot("th"), [])

    def test_publish_reaches_every_worker_of_the_language_only(self):
        broker = leader_broker()
        a, b, other = FakeSocket(), FakeSocket(), FakeSocket()
        broker.registry.add(worker("th", 1, a))
        broker.registry.add(worker("th", 2, b))
        broker.registry.add(worker("km", 1, other))
        broker._publish_translation({"type": "translated_chunk", "lang": "th", "text": "x"})
        self.assertEqual((len(a.frames), len(b.frames), len(other.frames)), (1, 1, 0))


class TelemetryTests(unittest.TestCase):
    def test_in_flight_jobs_of_a_vanished_worker_are_marked_interrupted(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1))
        for status in ("queued", "processing", "quota_wait", "retrying", "translating"):
            broker.update_job({"chunk_id": status, "lang": "km", "worker_id": "km-gone"}, status)
        broker.update_job({"chunk_id": "done", "lang": "km", "worker_id": "km-gone"}, "completed")
        jobs = {j["chunk_id"]: j for j in broker.telemetry()["jobs"]}
        for status in ("queued", "processing", "quota_wait", "retrying", "translating"):
            self.assertEqual(jobs[status]["status"], "interrupted")
        self.assertEqual(jobs["done"]["status"], "completed")
        self.assertIn("resubmit", jobs["queued"]["detail"])

    def test_jobs_of_a_registered_worker_keep_their_status(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1))
        broker.update_job({"chunk_id": "c", "lang": "th", "worker_id": "th-1"}, "processing")
        self.assertEqual(broker.telemetry()["jobs"][0]["status"], "processing")

    def test_members_report_language_mode_and_heartbeat_age(self):
        broker = leader_broker()
        broker.registry.add(worker("th", 1, mode="gemini"))
        member = broker.telemetry()["members"][0]
        self.assertEqual((member["lang"], member["mode"]), ("th", "gemini"))
        self.assertGreaterEqual(member["heartbeat_age"], 0)


class BackgroundLoopTests(unittest.TestCase):
    def test_reaper_expires_a_silent_worker_but_keeps_a_fresh_one(self):
        broker = leader_broker()
        stale, fresh = worker("th", 1), worker("th", 2)
        stale.last_seen = time.monotonic() - 100
        broker.registry.add(stale)
        broker.registry.add(fresh)
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.02):
            thread = threading.Thread(target=broker._worker_reaper, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: [w.worker_id for w in broker.registry.snapshot("th")] == ["th-2"]))
            broker.stop_event.set()
            thread.join(1)

    def test_healthy_leader_probe_never_starts_an_election(self):
        broker = Broker(1)
        broker.election.leader_id = 2
        probes = []

        def probe(peer, message):
            probes.append(peer)
            if len(probes) >= 3:
                broker.stop_event.set()
            return {"type": "heartbeat_ack", "is_leader": True}
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.01), \
                mock.patch.object(config, "HEARTBEAT_TIMEOUT", 0.03), \
                mock.patch.object(broker, "_peer_request", side_effect=probe), \
                mock.patch.object(broker.election, "start") as start:
            broker._leader_monitor()
        start.assert_not_called()

    def test_a_peer_that_answers_but_is_no_longer_leader_counts_as_failed(self):
        broker = Broker(1)
        broker.election.leader_id = 2
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.01), \
                mock.patch.object(config, "HEARTBEAT_TIMEOUT", 0.05), \
                mock.patch.object(broker, "_peer_request", return_value={"type": "heartbeat_ack", "is_leader": False}), \
                mock.patch.object(broker.election, "start", side_effect=broker.stop_event.set) as start:
            broker._leader_monitor()
        start.assert_called_once()

    def test_the_leader_never_probes_itself(self):
        broker = leader_broker()
        broker._peer_request = mock.Mock(side_effect=AssertionError("leader probed a peer"))
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.01):
            thread = threading.Thread(target=broker._leader_monitor, daemon=True)
            thread.start()
            time.sleep(0.1)
            broker.stop_event.set()
            thread.join(1)
        broker._peer_request.assert_not_called()


class LeadershipTests(unittest.TestCase):
    def test_peer_request_to_an_unknown_peer_is_none(self):
        self.assertIsNone(Broker(1)._peer_request(99, {"type": "heartbeat"}))

    def test_peer_request_to_an_unreachable_peer_is_none(self):
        broker = Broker(1)
        with mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError()):
            self.assertIsNone(broker._peer_request(2, {"type": "heartbeat"}))

    def test_announce_tells_every_peer(self):
        broker = Broker(2)
        with mock.patch.object(broker, "_peer_request") as request:
            broker._announce(2)
        request.assert_called_once_with(1, {"type": "leader_announce", "leader_id": 2})

    def test_demotion_disconnects_the_clients_of_a_previously_serving_leader(self):
        broker = leader_broker(1)
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        broker.election.leader_id = 2
        broker._leader_changed(2)
        self.assertEqual(broker.registry.all(), [])
        self.assertTrue(sock.closed)

    def test_becoming_leader_keeps_existing_state(self):
        broker = Broker(2)
        broker.registry.add(worker("th", 1))
        broker.election.leader_id = 2
        broker._leader_changed(2)
        self.assertEqual(len(broker.registry.all()), 1)

    def test_shutdown_is_idempotent_and_closes_clients(self):
        broker = leader_broker()
        broker.server = mock.Mock()
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        broker.shutdown()
        broker.shutdown()
        self.assertTrue(broker.stop_event.is_set())
        self.assertTrue(sock.closed)
        self.assertIsNone(broker.server)

    def test_redirect_names_the_leader_when_known(self):
        broker = Broker(1)
        broker.election.leader_id = 2
        self.assertEqual(broker._redirect()["leader"]["id"], 2)
        broker.election.leader_id = 77
        self.assertIsNone(broker._redirect()["leader"])


if __name__ == "__main__":
    unittest.main()
