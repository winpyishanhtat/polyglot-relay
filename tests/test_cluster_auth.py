"""The shared cluster secret (POLYGLOT_CLUSTER_SECRET): only processes that know it can join the relay."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

from common import protocol
from common.protocol import authenticate, encode, recv_one, sign
from test_broker_server import Session, leader_broker, wait_for
from translator_worker import TranslatorWorker

ROOT = Path(__file__).resolve().parents[1]


class SecretEnv(unittest.TestCase):
    SECRET = "correct horse battery staple"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": self.SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)


class SigningTests(unittest.TestCase):
    def test_without_a_secret_messages_are_unchanged_and_accepted(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": ""}):
            self.assertNotIn("auth", json.loads(encode({"type": "register"})))
            self.assertTrue(authenticate({"type": "register"}))

    def test_a_signed_message_is_accepted_once(self):
        message = sign({"type": "register", "role": "worker"}, "s")
        self.assertTrue(authenticate(message, "s"))
        self.assertFalse(authenticate(message, "s"))              # replayed

    def test_the_secret_itself_never_travels(self):
        wire = encode_with("a-very-secret-value", {"type": "register"})
        self.assertNotIn("a-very-secret-value", wire)

    def test_a_wrong_secret_or_none_is_refused(self):
        self.assertFalse(authenticate(sign({"type": "register"}, "other"), "s"))
        self.assertFalse(authenticate({"type": "register"}, "s"))

    def test_a_proof_cannot_be_moved_to_another_kind_of_message(self):
        proof = sign({"type": "heartbeat"}, "s")["auth"]
        self.assertFalse(authenticate({"type": "register", "auth": proof}, "s"))

    def test_an_old_or_future_proof_is_refused(self):
        message = sign({"type": "register"}, "s")
        self.assertFalse(authenticate(dict(message), "s", now=time.time() + protocol.AUTH_WINDOW_SECONDS + 5))
        message = sign({"type": "register"}, "s")
        self.assertFalse(authenticate(dict(message), "s", now=time.time() - protocol.AUTH_WINDOW_SECONDS - 5))

    def test_malformed_proofs_are_refused_not_raised(self):
        for proof in ("x", {}, {"ts": "soon", "nonce": "n", "mac": "m"}, {"ts": int(time.time()), "nonce": "", "mac": "m"}, None):
            self.assertFalse(authenticate({"type": "register", "auth": proof}, "s"), proof)

    def test_encode_signs_when_the_secret_is_set(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": "s"}):
            message = json.loads(encode({"type": "register"}))
            self.assertIn("auth", message)
            self.assertTrue(authenticate(message))


def encode_with(secret, message):
    with mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": secret}):
        return encode(message).decode("utf-8")


class BrokerTests(SecretEnv):
    def test_a_signed_worker_is_registered(self):
        broker = leader_broker(2)
        session = Session(broker)
        reply = session.request({"type": "register", "role": "worker", "lang": "th", "worker_id": "th-1",
                                 "host": "127.0.0.1", "port": 9})
        self.assertTrue(reply.get("accepted"), reply)
        session.close()

    def test_an_unsigned_connection_is_refused_and_closed(self):
        broker = leader_broker(2)
        session = Session(broker)
        raw = json.dumps({"type": "register", "role": "worker", "lang": "th", "worker_id": "intruder",
                          "host": "127.0.0.1", "port": 9}) + "\n"
        with self.assertLogs("polyglot.broker", level="WARNING") as logs:
            session.client.sendall(raw.encode())
            reply = session.recv()
        self.assertEqual(reply, {**reply, "type": "error", "error": "authentication failed"})
        self.assertTrue(any("missing or wrong cluster secret" in line for line in logs.output))
        self.assertEqual(broker.registry.snapshot("th"), [])
        session.close()

    def test_a_peer_with_another_secret_cannot_start_an_election(self):
        broker = leader_broker(2)
        session = Session(broker)
        with self.assertLogs("polyglot.broker", level="WARNING"):
            session.client.sendall(encode_with("wrong", {"type": "election", "candidate_id": 9}).encode())
            reply = session.recv()
        self.assertEqual(reply["type"], "error")
        self.assertEqual(broker.election.leader_id, 2)
        session.close()


class WorkerListenerTests(SecretEnv):
    def worker(self):
        worker = TranslatorWorker("th", "127.0.0.1", 0, "th-auth", mock=True)
        worker._start_listener_server()
        self.addCleanup(worker.shutdown)
        return worker

    def connect(self, worker):
        sock = socket.create_connection(("127.0.0.1", worker.port), timeout=3)
        self.addCleanup(sock.close)
        return sock

    def test_a_listener_that_proves_the_secret_receives_captions(self):
        worker = self.worker()
        sock = self.connect(worker)
        sock.sendall(encode({"type": "subscribe", "lang": "th"}))
        self.assertTrue(wait_for(lambda: len(worker.listeners) == 1))
        worker._broadcast({"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "x"})
        self.assertEqual(recv_one(sock)["chunk_id"], "c1")

    def test_a_silent_or_unsigned_listener_is_dropped(self):
        worker = self.worker()
        with self.assertLogs("polyglot.worker", level="WARNING"):
            unsigned = self.connect(worker)
            unsigned.sendall((json.dumps({"type": "subscribe", "lang": "th"}) + "\n").encode())
            self.assertEqual(unsigned.recv(10), b"")                  # closed on it
        self.assertEqual(len(worker.listeners), 0)

    def test_without_a_secret_listeners_need_not_say_anything(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_CLUSTER_SECRET": ""}):
            worker = self.worker()
            self.connect(worker)
            self.assertTrue(wait_for(lambda: len(worker.listeners) == 1))


class RealClusterTests(unittest.TestCase):
    """Real brokers and a real worker, all with the secret; an outsider without it is turned away."""

    PORTS = (17811, 17812)

    def setUp(self):
        self.env = {**os.environ, "POLYGLOT_CLUSTER_SECRET": "event-2026", "POLYGLOT_BROKER_COUNT": "2",
                    "POLYGLOT_BROKER_1_HOST": "127.0.0.1", "POLYGLOT_BROKER_2_HOST": "127.0.0.1",
                    "POLYGLOT_BROKER_1_PORT": str(self.PORTS[0]), "POLYGLOT_BROKER_2_PORT": str(self.PORTS[1]),
                    "POLYGLOT_HEARTBEAT_INTERVAL": "0.2", "POLYGLOT_HEARTBEAT_TIMEOUT": "0.6",
                    "POLYGLOT_ELECTION_TIMEOUT": "0.2", "POLYGLOT_CONNECT_TIMEOUT": "0.3"}
        self.processes = [subprocess.Popen([sys.executable, "broker.py", "--id", str(n)], cwd=ROOT, env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for n in (1, 2)]
        self.addCleanup(self.stop)

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def ask(self, port, message, secret):
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.settimeout(1)
            sock.sendall(encode_with(secret, message).encode())
            return recv_one(sock)

    def leader_members(self, secret="event-2026"):
        try:
            reply = self.ask(self.PORTS[1], {"type": "heartbeat", "sender_id": "dashboard", "role": "broker"}, secret)
        except OSError:
            return None
        return reply

    def test_members_with_the_secret_work_and_an_outsider_is_refused(self):
        self.assertTrue(wait_for(lambda: (self.leader_members() or {}).get("is_leader"), 10))
        worker = subprocess.Popen([sys.executable, "translator_worker.py", "--lang", "th", "--mock"], cwd=ROOT, env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(worker)
        self.assertTrue(wait_for(lambda: any(m.get("lang") == "th" for m in (self.leader_members() or {}).get("members", [])), 15))
        outsider = self.ask(self.PORTS[1], {"type": "heartbeat", "sender_id": "dashboard", "role": "broker"}, "guess")
        self.assertEqual(outsider["type"], "error")
        healthcheck = subprocess.run([sys.executable, "-m", "common.healthcheck", "--port", str(self.PORTS[0])], cwd=ROOT,
                                     env=self.env, capture_output=True, timeout=10)
        self.assertEqual(healthcheck.returncode, 0, healthcheck.stderr)


if __name__ == "__main__":
    unittest.main()
