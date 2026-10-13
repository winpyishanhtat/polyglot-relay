"""Unit tests for the shared building blocks in common/ and the listener client.

Network behaviour is exercised against throw-away TCP servers on localhost
rather than mocks, so framing, timeouts and error handling are the real ones.
"""

import contextlib
import io
import json
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import listener_client
from common import config, discovery, healthcheck
from common.election import BullyElection
from common.heartbeat import FailureDetector, HeartbeatSender
from common.protocol import MESSAGE_TYPES, ProtocolError, decode_stream, encode, recv_one


@contextlib.contextmanager
def tcp_server(handler):
    """Serve each connection with handler(conn) on a free localhost port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn):
        with conn:
            try:
                handler(conn)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        server.close()
        thread.join(timeout=1)


def reply_with(message):
    def handler(conn):
        recv_one(conn)
        conn.sendall(encode(message))
    return handler


class ConfigTests(unittest.TestCase):
    def test_nine_asean_languages_in_stable_order(self):
        self.assertEqual(config.TARGET_LANGUAGES, ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"])

    def test_every_language_has_name_native_and_group(self):
        for code, meta in config.LANGUAGES.items():
            self.assertTrue(meta["name"] and meta["native"] and meta["group"], code)

    def test_two_brokers_with_distinct_ids_and_ports(self):
        self.assertEqual([b["id"] for b in config.BROKERS], [1, 2])
        self.assertEqual(len({b["port"] for b in config.BROKERS}), 2)

    def test_broker_by_id_returns_a_copy(self):
        broker = config.broker_by_id(2)
        broker["port"] = 1
        self.assertNotEqual(config.broker_by_id(2)["port"], 1)

    def test_broker_by_id_rejects_unknown_id(self):
        with self.assertRaises(ValueError):
            config.broker_by_id(99)

    def test_language_label_includes_native_name_only_when_different(self):
        self.assertEqual(config.language_label("en"), "English")
        self.assertEqual(config.language_label("th"), "Thai (\u0e44\u0e17\u0e22)")

    def test_timeouts_leave_room_for_detection_before_staleness(self):
        self.assertLess(config.HEARTBEAT_INTERVAL, config.HEARTBEAT_TIMEOUT)


class ProtocolEdgeTests(unittest.TestCase):
    def test_encode_is_compact_utf8_with_newline(self):
        data = encode({"type": "heartbeat", "text": "\u1019\u103c\u1014\u103a\u1019\u102c"})
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)
        self.assertNotIn(b" ", data.replace("\u1019\u103c\u1014\u103a\u1019\u102c".encode(), b""))
        self.assertEqual(json.loads(data)["text"], "\u1019\u103c\u1014\u103a\u1019\u102c")

    def test_encode_rejects_non_dict_missing_or_unknown_type(self):
        for bad in ("text", {"no": "type"}, {"type": 3}, {"type": "launch_missiles"}):
            with self.assertRaises(ProtocolError, msg=repr(bad)):
                encode(bad)

    def test_every_documented_message_type_encodes(self):
        for kind in MESSAGE_TYPES:
            self.assertEqual(json.loads(encode({"type": kind}))["type"], kind)

    def test_decode_stream_splits_frames_and_ignores_blank_lines(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(encode({"type": "heartbeat"}) + b"\n\n" + encode({"type": "error"}))
            b.shutdown(socket.SHUT_WR)
            self.assertEqual([m["type"] for m in decode_stream(a)], ["heartbeat", "error"])

    def test_decode_stream_reassembles_a_frame_split_across_packets(self):
        a, b = socket.socketpair()
        data = encode({"type": "transcript_chunk", "text": "hello"})
        with a, b:
            def trickle():
                for i in range(0, len(data), 5):
                    b.sendall(data[i:i + 5])
                    time.sleep(0.01)
                b.shutdown(socket.SHUT_WR)
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            self.assertEqual(next(decode_stream(a))["text"], "hello")
            sender.join(2)

    def test_decode_stream_rejects_truncated_frame_on_close(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(b'{"type":"heartbeat"')
            b.shutdown(socket.SHUT_WR)
            with self.assertRaises(ProtocolError):
                list(decode_stream(a))

    def test_decode_stream_rejects_invalid_json_and_bad_types(self):
        for payload in (b"not json\n", b'{"type":"nope"}\n', b"[1,2]\n", b"\xff\xfe\n"):
            a, b = socket.socketpair()
            with a, b:
                b.sendall(payload)
                with self.assertRaises(ProtocolError, msg=payload):
                    next(decode_stream(a))

    def test_recv_one_leaves_following_frame_unread(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(encode({"type": "heartbeat"}) + encode({"type": "error"}))
            self.assertEqual(recv_one(a)["type"], "heartbeat")
            self.assertEqual(recv_one(a)["type"], "error")

    def test_recv_one_reads_a_large_frame_quickly(self):
        # the broker's job table reply is ~300 KB; reading it a byte at a time took ~1.6 s
        big = {"type": "heartbeat_ack", "jobs": [{"chunk_id": f"ui-{n}", "lang": "th", "status": "completed"} for n in range(6000)]}
        a, b = socket.socketpair()
        with a, b:
            sender = threading.Thread(target=lambda: b.sendall(encode(big) + encode({"type": "error"})), daemon=True)
            sender.start()
            started = time.monotonic()
            message = recv_one(a)
            elapsed = time.monotonic() - started
            sender.join(2)
            self.assertEqual(len(message["jobs"]), 6000)
            self.assertLess(elapsed, 0.25)         # a byte at a time took ~0.9 s here
            self.assertEqual(recv_one(a)["type"], "error")     # the next frame was left intact

    def test_recv_one_handles_a_frame_arriving_in_pieces_without_losing_the_next_one(self):
        a, b = socket.socketpair()
        data = encode({"type": "transcript_chunk", "text": "x" * 70000}) + encode({"type": "heartbeat"})
        with a, b:
            def trickle():
                for i in range(0, len(data), 9000):
                    b.sendall(data[i:i + 9000])
                    time.sleep(0.005)
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            self.assertEqual(len(recv_one(a)["text"]), 70000)
            self.assertEqual(recv_one(a)["type"], "heartbeat")
            sender.join(2)

    def test_recv_one_with_several_small_frames_already_buffered_takes_them_one_at_a_time(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(b"".join(encode({"type": "heartbeat", "n": n}) for n in range(50)))
            self.assertEqual([recv_one(a)["n"] for _ in range(50)], list(range(50)))

    def test_recv_one_frame_that_is_exactly_the_peek_size_still_ends_cleanly(self):
        a, b = socket.socketpair()
        with a, b:
            frame = encode({"type": "heartbeat", "pad": ""})
            pad = "p" * (65536 - len(frame))
            data = encode({"type": "heartbeat", "pad": pad})
            self.assertEqual(len(data), 65536)
            b.sendall(data + encode({"type": "error"}))
            self.assertEqual(len(recv_one(a)["pad"]), len(pad))
            self.assertEqual(recv_one(a)["type"], "error")

    def test_recv_one_an_empty_line_is_a_protocol_error(self):
        a, b = socket.socketpair()
        with a, b:
            b.sendall(b"\n")
            with self.assertRaises(ProtocolError):
                recv_one(a)

    def test_recv_one_reports_closed_peer(self):
        a, b = socket.socketpair()
        b.close()
        with a, self.assertRaises(ConnectionError):
            recv_one(a)

    def test_recv_one_rejects_oversized_frame(self):
        a, b = socket.socketpair()
        with a, b:
            def flood():
                try:
                    b.sendall(b"x" * 1_100_000)
                except OSError:
                    pass
            threading.Thread(target=flood, daemon=True).start()
            with self.assertRaises(ProtocolError):
                recv_one(a)


class BullyElectionTests(unittest.TestCase):
    def make(self, broker_id, peers, request, **kwargs):
        calls = {"announced": [], "changed": []}
        election = BullyElection(
            broker_id, peers, request,
            announce=calls["announced"].append,
            on_leader_change=calls["changed"].append,
            election_timeout=kwargs.get("timeout", 0.05))
        return election, calls

    def test_highest_id_wins_without_probing_anyone(self):
        request = mock.Mock()
        election, calls = self.make(3, [1, 2, 3], request)
        self.assertEqual(election.start(), 3)
        request.assert_not_called()
        self.assertEqual(calls["announced"], [3])
        self.assertEqual(calls["changed"], [3])

    def test_only_higher_ids_are_probed(self):
        probed = []

        def request(peer, message):
            probed.append((peer, message["candidate_id"]))
            return None
        election, _ = self.make(2, [1, 2, 3, 4], request)
        election.start()
        self.assertEqual(probed, [(3, 2), (4, 2)])

    def test_unreachable_higher_peers_mean_self_elected(self):
        election, calls = self.make(1, [1, 2], lambda peer, msg: None)
        self.assertEqual(election.start(), 1)
        self.assertEqual(calls["announced"], [1])

    def test_waits_for_announcement_when_a_higher_peer_answers_ok(self):
        election, calls = self.make(1, [1, 2], lambda peer, msg: {"type": "election_ok"}, timeout=1.0)
        threading.Timer(0.05, election.receive_announcement, args=(2,)).start()
        self.assertEqual(election.start(), 2)
        self.assertEqual(calls["announced"], [])
        self.assertEqual(election.leader_id, 2)

    def test_restarts_and_wins_when_the_announcing_peer_died(self):
        answers = iter([{"type": "election_ok"}, None, None, None])
        election, calls = self.make(1, [1, 2], lambda peer, msg: next(answers), timeout=0.02)
        self.assertEqual(election.start(), 1)
        self.assertEqual(calls["announced"], [1])

    def test_receive_announcement_records_leader_and_notifies(self):
        election, calls = self.make(1, [1, 2], lambda peer, msg: None)
        election.receive_announcement(2)
        self.assertEqual(election.leader_id, 2)
        self.assertEqual(calls["changed"], [2])

    def test_lower_candidate_triggers_own_election_but_higher_does_not(self):
        started = threading.Event()
        election, _ = self.make(2, [1, 2, 3], lambda peer, msg: None)
        with mock.patch.object(election, "start", side_effect=started.set):
            election.receive_election(3)
            self.assertFalse(started.wait(0.1))
            election.receive_election(1)
            self.assertTrue(started.wait(1.0))

    def test_second_concurrent_election_returns_instead_of_stacking(self):
        gate, entered = threading.Event(), threading.Event()

        def request(peer, message):
            entered.set()
            gate.wait(2)
            return None
        election, _ = self.make(1, [1, 2], request)
        results = []
        first = threading.Thread(target=lambda: results.append(election.start()))
        first.start()
        self.assertTrue(entered.wait(1))
        self.assertIsNone(election.start())  # no leader known yet; must not block or recurse
        gate.set()
        first.join(2)
        self.assertEqual(results, [1])


class HeartbeatTests(unittest.TestCase):
    def test_sender_sends_repeatedly_until_stopped(self):
        sent = []
        sender = HeartbeatSender(lambda: sent.append(1), 0.01)
        sender.start()
        time.sleep(0.15)
        sender.stop()
        count = len(sent)
        self.assertGreaterEqual(count, 3)
        time.sleep(0.05)
        self.assertEqual(len(sent), count)

    def test_sender_quits_quietly_when_the_connection_breaks(self):
        calls = []

        def send():
            calls.append(1)
            raise ConnectionError("gone")
        sender = HeartbeatSender(send, 0.01)
        sender.start()
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)
        sender.stop()

    def test_stop_before_start_is_safe(self):
        HeartbeatSender(lambda: None, 1).stop()

    def test_detector_expires_only_after_timeout_and_seen_resets_it(self):
        detector = FailureDetector(0.1)
        self.assertFalse(detector.expired())
        time.sleep(0.15)
        self.assertTrue(detector.expired())
        detector.seen()
        self.assertFalse(detector.expired())

    def test_detector_uses_wall_clock_not_poll_count(self):
        detector = FailureDetector(60)
        for _ in range(1000):
            self.assertFalse(detector.expired())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Windows takes ~1.5s to report a refused connection unless the timeout is short.
        patcher = mock.patch.object(config, "CONNECT_TIMEOUT", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def brokers(self, *ports):
        return [{"id": i + 1, "host": "127.0.0.1", "port": port} for i, port in enumerate(ports)]

    def closed_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_registers_with_the_first_broker_that_accepts(self):
        with tcp_server(reply_with({"type": "register_ack", "accepted": True, "leader_id": 1})) as port:
            with mock.patch.object(config, "BROKERS", self.brokers(port)):
                sock, ack = discovery.connect_to_leader({"type": "register", "role": "worker"})
                sock.close()
        self.assertTrue(ack["accepted"])

    def test_skips_a_follower_and_finds_the_leader(self):
        follower = reply_with({"type": "register_ack", "accepted": False, "leader_id": 2})
        leader = reply_with({"type": "register_ack", "accepted": True, "leader_id": 2})
        with tcp_server(follower) as p1, tcp_server(leader) as p2:
            with mock.patch.object(config, "BROKERS", self.brokers(p1, p2)):
                sock, ack = discovery.connect_to_leader({"type": "register"})
                sock.close()
        self.assertEqual(ack["leader_id"], 2)

    def test_skips_a_dead_broker(self):
        with tcp_server(reply_with({"type": "register_ack", "accepted": True})) as live:
            with mock.patch.object(config, "BROKERS", self.brokers(self.closed_port(), live)):
                sock, _ = discovery.connect_to_leader({"type": "register"})
                sock.close()

    def test_raises_with_every_reason_when_nobody_leads(self):
        follower = reply_with({"type": "register_ack", "accepted": False})
        with tcp_server(follower) as live:
            with mock.patch.object(config, "BROKERS", self.brokers(self.closed_port(), live)):
                with self.assertRaises(ConnectionError) as ctx:
                    discovery.connect_to_leader({"type": "register"})
        self.assertIn("broker 1", str(ctx.exception))
        self.assertIn("broker 2 is not leader", str(ctx.exception))

    def test_garbage_reply_counts_as_unavailable(self):
        def garbage(conn):
            conn.recv(1024)
            conn.sendall(b"definitely not json\n")
        with tcp_server(garbage) as port:
            with mock.patch.object(config, "BROKERS", self.brokers(port)):
                with self.assertRaises(ConnectionError):
                    discovery.connect_to_leader({"type": "register"})

    def test_discover_worker_returns_endpoint(self):
        info = {"type": "worker_info", "worker_id": "th-1", "host": "h", "port": 8100}
        with tcp_server(reply_with(info)) as port:
            with mock.patch.object(config, "BROKERS", self.brokers(port)):
                self.assertEqual(discovery.discover_worker("th")["worker_id"], "th-1")

    def test_discover_worker_sends_the_requested_language(self):
        seen = []

        def handler(conn):
            seen.append(recv_one(conn))
            conn.sendall(encode({"type": "worker_info", "worker_id": "x"}))
        with tcp_server(handler) as port:
            with mock.patch.object(config, "BROKERS", self.brokers(port)):
                discovery.discover_worker("km")
        self.assertEqual(seen[0], {"type": "discover_worker", "lang": "km"})

    def test_discover_worker_none_when_no_worker_registered(self):
        with tcp_server(reply_with({"type": "worker_info", "worker_id": None})) as port:
            with mock.patch.object(config, "BROKERS", self.brokers(port)):
                self.assertIsNone(discovery.discover_worker("th"))

    def test_discover_worker_falls_through_to_second_broker(self):
        info = {"type": "worker_info", "worker_id": "lo-1", "host": "h", "port": 1}
        with tcp_server(reply_with(info)) as live:
            with mock.patch.object(config, "BROKERS", self.brokers(self.closed_port(), live)):
                self.assertEqual(discovery.discover_worker("lo")["worker_id"], "lo-1")

    def test_discover_worker_none_when_all_brokers_down(self):
        with mock.patch.object(config, "BROKERS", self.brokers(self.closed_port(), self.closed_port())):
            self.assertIsNone(discovery.discover_worker("th"))


class HealthcheckTests(unittest.TestCase):
    def run_main(self, port):
        with mock.patch.object(sys, "argv", ["healthcheck", "--port", str(port)]):
            healthcheck.main()

    def test_passes_when_broker_acks_a_heartbeat(self):
        with tcp_server(reply_with({"type": "heartbeat_ack"})) as port:
            self.run_main(port)

    def test_exits_nonzero_on_any_other_reply(self):
        with tcp_server(reply_with({"type": "error"})) as port:
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(port)
        self.assertEqual(ctx.exception.code, 1)

    def test_raises_when_nothing_is_listening(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(OSError):
            self.run_main(port)


class ListenerClientTests(unittest.TestCase):
    def test_prints_each_translated_chunk_with_a_timestamp_and_ignores_others(self):
        def worker(conn):
            conn.sendall(encode({"type": "heartbeat"}))
            conn.sendall(encode({"type": "translated_chunk", "text": "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35", "timestamp": 0}))
            conn.sendall(encode({"type": "translated_chunk", "text": "\u0e25\u0e32\u0e01\u0e48\u0e2d\u0e19", "timestamp": 0}))
        stop = RuntimeError("stop the reconnect loop")
        with tcp_server(worker) as port:
            endpoint = {"worker_id": "th-1", "host": "127.0.0.1", "port": port}
            out = io.StringIO()
            with mock.patch.object(listener_client, "discover_worker", return_value=endpoint), \
                    mock.patch.object(listener_client.time, "sleep", side_effect=stop), \
                    contextlib.redirect_stdout(out):
                with self.assertRaises(RuntimeError):
                    listener_client.listen("th")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("] \u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35"))
        self.assertRegex(lines[0], r"^\[\d\d:\d\d:\d\d\] ")

    def test_retries_discovery_while_no_worker_is_registered(self):
        answers = iter([None, None])
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise RuntimeError("done")
        with mock.patch.object(listener_client, "discover_worker", side_effect=lambda lang: next(answers)), \
                mock.patch.object(listener_client.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(RuntimeError):
                listener_client.listen("th")
        self.assertEqual(sleeps, [config.RECONNECT_DELAY] * 2)

    def test_reconnects_via_discovery_after_the_worker_drops(self):
        endpoint = {"worker_id": "th-1", "host": "127.0.0.1", "port": 1}
        discoveries = []

        def discover(lang):
            discoveries.append(lang)
            return endpoint
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise RuntimeError("done")
        with mock.patch.object(listener_client, "discover_worker", side_effect=discover), \
                mock.patch.object(listener_client.socket, "create_connection", side_effect=ConnectionRefusedError()), \
                mock.patch.object(listener_client.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(RuntimeError):
                listener_client.listen("th")
        self.assertEqual(discoveries, ["th", "th"])


if __name__ == "__main__":
    unittest.main()
