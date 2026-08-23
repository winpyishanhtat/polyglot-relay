import json
import unittest
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

if __name__ == "__main__": unittest.main()
