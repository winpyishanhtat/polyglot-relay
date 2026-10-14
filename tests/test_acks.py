"""Producer acknowledgements: a chunk is kept by the producer until the leader says it has routed and mirrored it,
so a leader that dies after reading a chunk costs nothing - the chunk is sent again to the next leader."""

import socket
import threading
import time
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import stt_service
from common import config
from common.protocol import decode_stream, encode, recv_one
from stt_service import TranscriptProducer
from test_broker_server import FakeSocket, Session, leader_broker, wait_for, worker


def chunk(n=1, **extra):
    return {"type": "transcript_chunk", "chunk_id": f"c{n}", "text": f"text {n}", "source_lang": "en",
            "targets": ["th"], **extra}


class LeaderStub:
    """A stand-in leader for a producer: registers it, records every chunk, optionally never acknowledges
    and drops the connection (a leader that dies after reading a chunk but before routing it)."""

    def __init__(self, acknowledge=True, drop_after=None):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.acknowledge, self.drop_after = acknowledge, drop_after
        self.received = []
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
            recv_one(conn)
            conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": 1}))
            for message in decode_stream(conn):
                self.received.append(message)
                if self.drop_after is not None and len(self.received) >= self.drop_after:
                    conn.close()
                    return
                if self.acknowledge and message.get("ack_id"):
                    conn.sendall(encode({"type": "chunk_ack", "chunk_id": message["chunk_id"], "ack_id": message["ack_id"]}))
        except Exception:
            pass

    def close(self):
        self.stop.set()
        self.server.close()


class ProducerAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(config, "RECONNECT_DELAY", 0.05), mock.patch.object(config, "CONNECT_TIMEOUT", 0.5)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def brokers(self, *stubs):
        patcher = mock.patch.object(config, "BROKERS", [{"id": n, "host": "127.0.0.1", "port": s.port} for n, s in enumerate(stubs, 1)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub(self, **kwargs):
        stub = LeaderStub(**kwargs)
        self.addCleanup(stub.close)
        return stub

    def producer(self):
        producer = TranscriptProducer()
        self.addCleanup(producer.close)
        return producer

    def test_every_chunk_carries_an_acknowledgement_id_that_the_broker_echoes_back(self):
        stub = self.stub()
        self.brokers(stub)
        producer = self.producer()
        producer.send("hello", "c1")
        self.assertTrue(wait_for(lambda: producer.unacknowledged() == 0))
        self.assertEqual(len(stub.received), 1)
        self.assertTrue(stub.received[0]["ack_id"])

    def test_a_chunk_stays_remembered_until_it_is_acknowledged(self):
        stub = self.stub(acknowledge=False)
        self.brokers(stub)
        producer = self.producer()
        producer.send("a", "c1")
        producer.send("b", "c2")
        self.assertTrue(wait_for(lambda: len(stub.received) == 2))
        time.sleep(0.2)
        self.assertEqual(producer.unacknowledged(), 2)

    def test_a_chunk_a_dying_leader_read_but_never_acknowledged_is_sent_again_to_the_next(self):
        dying = self.stub(acknowledge=False, drop_after=1)         # reads the chunk, then dies without routing it
        survivor = self.stub()
        self.brokers(dying, survivor)
        producer = self.producer()
        producer.send("important", "c1")
        self.assertTrue(wait_for(lambda: len(dying.received) == 1))
        dying.close()                                              # the first leader is gone for good
        producer.send("next", "c2")                                # the producer notices on its next send
        self.assertTrue(wait_for(lambda: {m["chunk_id"] for m in survivor.received} == {"c1", "c2"}, 5),
                        [m["chunk_id"] for m in survivor.received])
        self.assertTrue(wait_for(lambda: producer.unacknowledged() == 0))

    def test_acknowledged_chunks_are_not_resent_after_a_reconnect(self):
        first, second = self.stub(), self.stub()
        self.brokers(first, second)
        producer = self.producer()
        producer.send("done", "c1")
        self.assertTrue(wait_for(lambda: producer.unacknowledged() == 0))
        first.close()
        producer.sock.close()                                       # force a reconnect on the next send
        producer.sock = None
        producer.send("later", "c2")
        self.assertTrue(wait_for(lambda: len(second.received) >= 1))
        time.sleep(0.2)
        self.assertEqual([m["chunk_id"] for m in second.received], ["c2"])

    def test_the_current_chunk_is_sent_once_when_a_send_fails_and_the_producer_reconnects(self):
        stub = self.stub()
        self.brokers(stub)
        producer = self.producer()
        producer.send("first", "c1")
        self.assertTrue(wait_for(lambda: producer.unacknowledged() == 0))
        producer.sock.close()                                       # the next sendall raises
        producer.send("second", "c2")
        self.assertTrue(wait_for(lambda: producer.unacknowledged() == 0))
        self.assertEqual([m["chunk_id"] for m in stub.received].count("c2"), 1)

    def test_the_two_stages_of_one_chunk_are_tracked_separately(self):
        stub = self.stub(acknowledge=False)
        self.brokers(stub)
        producer = self.producer()
        producer.send("x", "c1", targets=["km", "my"])
        producer.send("x", "c1", pivot_en="Hello", targets=["th"])
        self.assertTrue(wait_for(lambda: len(stub.received) == 2))
        self.assertEqual(len({m["ack_id"] for m in stub.received}), 2)
        self.assertEqual(producer.unacknowledged(), 2)

    def test_the_memory_of_unacknowledged_chunks_is_bounded_in_count_and_age(self):
        stub = self.stub(acknowledge=False)
        self.brokers(stub)
        producer = self.producer()
        with mock.patch.object(stt_service, "UNACKED_MAX", 5):
            for n in range(12):
                producer.send("t", f"c{n}")
        self.assertEqual(producer.unacknowledged(), 5)
        with mock.patch.object(stt_service, "UNACKED_MAX_AGE", 0.05):
            time.sleep(0.1)
            producer.send("t", "fresh")
        self.assertEqual(producer.unacknowledged(), 1)


class BrokerAcknowledgementTests(unittest.TestCase):
    def test_the_leader_acknowledges_a_chunk_after_routing_and_replicating_it(self):
        broker = leader_broker(2)
        order = []
        broker.registry.add(worker("th", 1, FakeSocket()))
        with mock.patch.object(broker, "route_chunk", side_effect=lambda m: order.append("route")), \
                mock.patch.object(broker, "_replicate_flush", side_effect=lambda: order.append("replicate")):
            stt = Session(broker)
            stt.request({"type": "register", "role": "stt"})
            stt.send({**chunk(1), "ack_id": "abc123"})
            ack = stt.recv()
        stt.close()
        self.assertEqual((ack["type"], ack["chunk_id"], ack["ack_id"]), ("chunk_ack", "c1", "abc123"))
        self.assertEqual(order, ["route", "replicate"])

    def test_a_chunk_without_an_ack_id_is_routed_but_not_acknowledged(self):
        broker = leader_broker(2)
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        stt.send(chunk(1))                                      # an older producer
        self.assertTrue(wait_for(lambda: len([f for f in sock.frames if f["type"] == "transcript_chunk"]) == 1))
        stt.client.settimeout(0.3)
        with self.assertRaises((socket.timeout, TimeoutError)):
            stt.recv()
        stt.close()

    def test_a_demoted_broker_does_not_acknowledge(self):
        broker = leader_broker(1)
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        broker.election.leader_id = 2
        stt.send({**chunk(1), "ack_id": "zzz"})
        with self.assertRaises((ConnectionError, OSError)):
            stt.recv()
        stt.close()

    def test_every_chunk_is_acknowledged_individually_in_order(self):
        broker = leader_broker(2)
        broker.registry.add(worker("th", 1, FakeSocket()))
        stt = Session(broker)
        stt.request({"type": "register", "role": "stt"})
        for n in range(5):
            stt.send({**chunk(n), "ack_id": f"id{n}"})
        self.assertEqual([stt.recv()["ack_id"] for _ in range(5)], [f"id{n}" for n in range(5)])
        stt.close()


if __name__ == "__main__":
    unittest.main()
