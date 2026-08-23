import socket
import unittest

from common.protocol import MESSAGE_TYPES, decode_stream, encode, recv_one


class ProtocolTests(unittest.TestCase):
    def test_all_message_types_round_trip(self):
        left, right = socket.socketpair()
        try:
            expected = [{"type": kind, "value": "日本語 မြန်မာ"} for kind in sorted(MESSAGE_TYPES)]
            left.sendall(b"".join(encode(item) for item in expected))
            left.shutdown(socket.SHUT_WR)
            self.assertEqual(list(decode_stream(right)), expected)
        finally:
            left.close(); right.close()

    def test_recv_one_does_not_consume_second_frame(self):
        left, right = socket.socketpair()
        try:
            first = {"type": "heartbeat", "sender_id": 1, "role": "broker"}
            second = {"type": "election", "candidate_id": 1}
            left.sendall(encode(first) + encode(second)); left.shutdown(socket.SHUT_WR)
            self.assertEqual(recv_one(right), first)
            self.assertEqual(list(decode_stream(right)), [second])
        finally:
            left.close(); right.close()


if __name__ == "__main__": unittest.main()
