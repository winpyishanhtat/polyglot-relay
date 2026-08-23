"""Leader and worker discovery helpers used by clients."""

from __future__ import annotations

import socket
from typing import Any

from common import config
from common.protocol import ProtocolError, encode, recv_one


def connect_to_leader(registration: dict[str, Any]) -> tuple[socket.socket, dict]:
    """Connect and register with the first broker that confirms leadership."""
    errors: list[str] = []
    for broker in config.BROKERS:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((broker["host"], broker["port"]), config.CONNECT_TIMEOUT)
            sock.settimeout(config.CONNECT_TIMEOUT)
            sock.sendall(encode(registration))
            response = recv_one(sock)
            if response.get("type") == "register_ack" and response.get("accepted"):
                sock.settimeout(None)
                return sock, response
            errors.append(f"broker {broker['id']} is not leader")
        except (OSError, ConnectionError, ProtocolError) as exc:
            errors.append(f"broker {broker['id']}: {exc}")
        if sock:
            sock.close()
    raise ConnectionError("no leader broker available (" + "; ".join(errors) + ")")


def discover_worker(lang: str) -> dict | None:
    """Query the current leader for one round-robin-selected worker endpoint."""
    for broker in config.BROKERS:
        try:
            with socket.create_connection((broker["host"], broker["port"]), config.CONNECT_TIMEOUT) as sock:
                sock.settimeout(config.CONNECT_TIMEOUT)
                sock.sendall(encode({"type": "discover_worker", "lang": lang}))
                response = recv_one(sock)
                if response.get("type") == "worker_info":
                    return response if response.get("worker_id") else None
        except (OSError, ConnectionError, ProtocolError):
            continue
    return None

