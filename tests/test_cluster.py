import json
import os
import socket
import subprocess
import sys
import time
import unittest
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
        env.update(POLYGLOT_BROKER_1_PORT=str(PORT1), POLYGLOT_BROKER_2_PORT=str(PORT2),
                   POLYGLOT_HEARTBEAT_INTERVAL="0.2", POLYGLOT_HEARTBEAT_TIMEOUT="0.6",
                   POLYGLOT_ELECTION_TIMEOUT="0.2", POLYGLOT_CONNECT_TIMEOUT="0.2")
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

if __name__ == "__main__": unittest.main()
