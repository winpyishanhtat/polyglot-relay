import json
import threading
import time
import unittest
from unittest import mock

from common import config
from broker import Broker, WorkerConnection

class FakeSocket:
    def __init__(self): self.frames = []; self.closed = False
    def sendall(self, payload): self.frames.append(json.loads(payload.decode()))
    def close(self): self.closed = True

class BrokerRoutingTests(unittest.TestCase):
    def test_round_robin_is_independent_per_language(self):
        broker = Broker(1)
        en1, en2, th = FakeSocket(), FakeSocket(), FakeSocket()
        broker.registry.add(WorkerConnection("en-1", "en", "localhost", 1, en1))
        broker.registry.add(WorkerConnection("en-2", "en", "localhost", 2, en2))
        broker.registry.add(WorkerConnection("th-1", "th", "localhost", 3, th))
        for index in range(4):
            broker.route_chunk({"type": "transcript_chunk", "chunk_id": str(index), "text": "hello", "timestamp": index})
        self.assertEqual(len(en1.frames), 2); self.assertEqual(len(en2.frames), 2)
        self.assertEqual(len(th.frames), 4)
        self.assertEqual([f["chunk_id"] for f in en1.frames], ["0", "2"])


class TargetFilterTests(unittest.TestCase):
    """A chunk may name the languages it is for ("targets"), so a producer can
    send it in two stages; a chunk without the field still goes to everyone."""

    def _broker(self):
        broker = Broker(1)
        sockets = {lang: FakeSocket() for lang in ("en", "th", "km", "my")}
        for index, (lang, sock) in enumerate(sockets.items()):
            broker.registry.add(WorkerConnection(f"{lang}-1", lang, "localhost", index, sock))
        return broker, sockets

    def test_only_the_named_languages_receive_the_chunk(self):
        broker, sockets = self._broker()
        broker.route_chunk({"type": "transcript_chunk", "chunk_id": "1", "text": "x", "source_lang": "my",
                            "targets": ["km", "my"]})
        self.assertEqual({lang: len(s.frames) for lang, s in sockets.items()}, {"en": 0, "th": 0, "km": 1, "my": 1})
        # the source language (my) is in targets, so it still gets its passthrough
        self.assertEqual(sockets["my"].frames[0]["translation_backend"], "passthrough")

    def test_the_source_passthrough_is_not_repeated_by_the_second_stage(self):
        broker, sockets = self._broker()
        broker.route_chunk({"type": "transcript_chunk", "chunk_id": "1", "text": "x", "source_lang": "my",
                            "targets": ["en", "th"]})
        self.assertEqual(len(sockets["my"].frames), 0)
        self.assertEqual({lang: len(sockets[lang].frames) for lang in ("en", "th")}, {"en": 1, "th": 1})

    def test_a_chunk_without_targets_still_goes_to_every_language(self):
        broker, sockets = self._broker()
        broker.route_chunk({"type": "transcript_chunk", "chunk_id": "1", "text": "x", "source_lang": "en"})
        self.assertEqual({lang: len(s.frames) for lang, s in sockets.items() if lang != "en"}, {"th": 1, "km": 1, "my": 1})


class LeaderMonitorTests(unittest.TestCase):
    def test_slow_dead_leader_probes_do_not_delay_election_beyond_the_timeout(self):
        # Each probe to the dead leader blocks 0.3s (like a connect timeout).
        # Counting failed polls instead of elapsed time would need
        # TIMEOUT/INTERVAL = 6 probes (~2.4s); wall-clock detection fires
        # after ~TIMEOUT.
        broker = Broker(1)
        broker.election.leader_id = 2
        triggered = threading.Event()
        started = time.monotonic()

        def slow_dead_probe(peer_id, message):
            time.sleep(0.3)
            return None

        def record_election():
            triggered.set()
            broker.stop_event.set()

        broker._peer_request = slow_dead_probe
        broker.election.start = record_election
        with mock.patch.object(config, "HEARTBEAT_INTERVAL", 0.1), \
                mock.patch.object(config, "HEARTBEAT_TIMEOUT", 0.6):
            thread = threading.Thread(target=broker._leader_monitor, daemon=True)
            thread.start()
            self.assertTrue(triggered.wait(5), "election was never triggered")
            thread.join(2)
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__": unittest.main()
