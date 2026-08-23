"""Small Docker health check for a broker TCP endpoint."""

from __future__ import annotations

import argparse
import socket

from common.protocol import encode, recv_one


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with socket.create_connection((args.host, args.port), timeout=1) as sock:
        sock.settimeout(1)
        sock.sendall(encode({"type": "heartbeat", "sender_id": 0, "role": "broker"}))
        response = recv_one(sock)
        if response.get("type") != "heartbeat_ack":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
