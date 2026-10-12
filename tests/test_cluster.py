import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT1, PORT2 = 17501, 17502
BUFFERS = {}

def send(sock, message):
    sock.sendall((json.dumps(message) + "\n").encode())

def receive(sock):
    data = BUFFERS.pop(sock.fileno(), bytearray())
    while b"\n" not in data:
        chunk = sock.recv(4096)
        if not chunk: raise ConnectionError("closed")
        data.extend(chunk)
    line, _, remainder = data.partition(b"\n")
    if remainder:
        BUFFERS[sock.fileno()] = bytearray(remainder)
    return json.loads(line)

def request(port, message):
    with socket.create_connection(("127.0.0.1", port), 0.5) as sock:
        send(sock, message); return receive(sock)

class ClusterIntegrationTests(unittest.TestCase):
    def setUp(self):
        env = os.environ.copy()
        # This test starts its own brokers on localhost. Pin the hosts so it also passes inside the
        # Compose test container, which sets them to the `broker-1` / `broker-2` service names.
        env.update(POLYGLOT_BROKER_1_HOST="127.0.0.1", POLYGLOT_BROKER_2_HOST="127.0.0.1",
                   POLYGLOT_BROKER_1_PORT=str(PORT1), POLYGLOT_BROKER_2_PORT=str(PORT2),
                   POLYGLOT_HEARTBEAT_INTERVAL="0.2", POLYGLOT_HEARTBEAT_TIMEOUT="0.6",
                   POLYGLOT_ELECTION_TIMEOUT="0.2", POLYGLOT_CONNECT_TIMEOUT="0.2")
        self.env = env
        self.processes = [subprocess.Popen([sys.executable, "broker.py", "--id", str(i)], cwd=ROOT,
                                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                          for i in (1, 2)]
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if request(PORT1, {"type": "heartbeat", "sender_id": 9, "role": "broker"}) and request(PORT2, {"type": "heartbeat", "sender_id": 9, "role": "broker"}): return
            except OSError: time.sleep(0.05)
        self.fail("brokers did not start")

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None: process.terminate()
        for process in self.processes:
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill()

    def wait_for_leader(self, port, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = request(port, {"type": "heartbeat", "sender_id": 9, "role": "broker"})
                if result.get("is_leader"): return
            except OSError: pass
            time.sleep(0.05)
        self.fail(f"port {port} did not become leader")

    def test_election_round_robin_and_failover(self):
        self.wait_for_leader(PORT2)
        workers = []
        for number in (1, 2):
            sock = socket.create_connection(("127.0.0.1", PORT2), 1); sock.settimeout(2)
            send(sock, {"type": "register", "role": "worker", "lang": "en", "worker_id": f"en-{number}",
                        "host": "127.0.0.1", "port": 18000 + number})
            self.assertTrue(receive(sock)["accepted"]); workers.append(sock)
        stt = socket.create_connection(("127.0.0.1", PORT2), 1); stt.settimeout(2)
        send(stt, {"type": "register", "role": "stt", "worker_id": "test-stt"}); self.assertTrue(receive(stt)["accepted"])
        for index in range(4):
            send(stt, {"type": "transcript_chunk", "chunk_id": str(index), "text": "hello", "timestamp": time.time()})
        self.assertEqual([receive(workers[0])["chunk_id"] for _ in range(2)], ["0", "2"])
        self.assertEqual([receive(workers[1])["chunk_id"] for _ in range(2)], ["1", "3"])
        stt.close()
        for sock in workers: sock.close()
        self.processes[1].terminate(); self.processes[1].wait(timeout=3)
        self.wait_for_leader(PORT1, timeout=4)

    def test_dashboard_eight_translations_and_source_with_real_worker_processes(self):
        from common.config import TARGET_LANGUAGES
        self.wait_for_leader(PORT2)
        for lang in TARGET_LANGUAGES:
            self.processes.append(subprocess.Popen(
                [sys.executable, "translator_worker.py", "--lang", lang, "--mock"],
                cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.processes.append(subprocess.Popen(
            [sys.executable, "web_ui.py", "--port", "17503"], cwd=ROOT, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        def status():
            with urllib.request.urlopen("http://127.0.0.1:17503/api/status", timeout=2) as response:
                return json.load(response)
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                if all(w["connected"] for w in status()["workers"].values()):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            self.fail("dashboard did not connect to all nine pools")
        body = json.dumps({"text": "hello", "source_lang": "my"}).encode()
        with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:17503/api/captions",
                data=body, headers={"Content-Type": "application/json"}), timeout=2) as response:
            self.assertEqual(response.status, 202)
        deadline = time.time() + 8
        while time.time() < deadline:
            result = status()
            if result["chunks"] and all(result["captions"].values()):
                break
            time.sleep(0.1)
        self.assertTrue(all(result["captions"].values()), result)
        states = result["chunks"][0]["targets"]
        self.assertEqual(states["my"]["status"], "source")
        self.assertEqual(sum(s["status"] == "completed" for s in states.values()), 8)
        self.assertEqual(result["captions"]["my"][0]["text"], "hello")
        self.assertEqual(len(next(b for b in result["brokers"] if b["leader"])["members"]), 9)

if __name__ == "__main__": unittest.main()
