"""A listener that connects gets the captions its worker broadcast just before, so a caption finished while it
was reconnecting (a worker restart, a dashboard restart, a job replayed after a broker failover) is not lost
on the way to the screen."""

import os
import socket
import time
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import translator_worker
from common.protocol import encode, recv_one
from test_broker_server import wait_for
from translator_worker import TranslatorWorker
from web_ui import DashboardState


def caption(n, age=0.0):
    return {"type": "translated_chunk", "chunk_id": f"c{n}", "lang": "th", "text": f"t{n}", "timestamp": time.time() - age}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = TranslatorWorker("th", "127.0.0.1", 0, "th-replay", mock=True)
        self.worker._start_listener_server()
        self.addCleanup(self.worker.shutdown)

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.worker.port), timeout=3)
        self.addCleanup(sock.close)
        return sock

    def test_a_caption_broadcast_just_before_a_listener_connects_is_sent_to_it(self):
        self.worker._broadcast(caption(1))                       # nobody listening yet
        sock = self.connect()
        self.assertEqual(recv_one(sock)["chunk_id"], "c1")
        self.assertTrue(wait_for(lambda: len(self.worker.listeners) == 1))
        self.worker._broadcast(caption(2))
        self.assertEqual(recv_one(sock)["chunk_id"], "c2")       # and later ones arrive once, in order

    def test_old_captions_are_not_replayed(self):
        self.worker._broadcast(caption(1, age=translator_worker.LISTENER_REPLAY_SECONDS + 5))
        self.worker._broadcast(caption(2))
        sock = self.connect()
        self.assertEqual(recv_one(sock)["chunk_id"], "c2")
        sock.settimeout(0.3)
        with self.assertRaises((socket.timeout, TimeoutError)):
            recv_one(sock)

    def test_replay_can_be_switched_off(self):
        self.worker._broadcast(caption(1))
        with mock.patch.object(translator_worker, "LISTENER_REPLAY_SECONDS", 0):
            sock = self.connect()
            self.assertTrue(wait_for(lambda: len(self.worker.listeners) == 1))
        sock.settimeout(0.3)
        with self.assertRaises((socket.timeout, TimeoutError)):
            recv_one(sock)

    def test_a_listener_admitted_with_the_cluster_secret_gets_the_replay_too(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": "s"}):
            self.worker._broadcast(caption(1))
            sock = self.connect()
            sock.sendall(encode({"type": "subscribe", "lang": "th"}))
            self.assertEqual(recv_one(sock)["chunk_id"], "c1")

    def test_only_a_bounded_number_of_recent_captions_is_kept(self):
        for n in range(400):
            self.worker._broadcast(caption(n))
        self.assertEqual(len(self.worker._recent_captions), 300)


class DuplicateDisplayTests(unittest.TestCase):
    def test_the_dashboard_shows_a_replayed_caption_once(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": ""}):
            state = DashboardState()
        state.record_original("c1", "hello", "en")
        for _ in range(2):
            state.add_caption({"lang": "th", "chunk_id": "c1", "text": "t1"})
        self.assertEqual(len(state.captions["th"]), 1)
        self.assertEqual(state.history["c1"]["translations"]["th"]["text"], "t1")


if __name__ == "__main__":
    unittest.main()
