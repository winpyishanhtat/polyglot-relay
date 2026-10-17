"""Newline-delimited JSON wire protocol.

With POLYGLOT_CLUSTER_SECRET set, every message carries an "auth" proof: an HMAC-SHA256 over a timestamp, a
one-time nonce and the message type, keyed with the shared secret (the secret itself is never sent). Whoever
accepts a connection checks the first message with authenticate(): a missing, wrong, stale or replayed proof
is refused, so a process that does not know the secret cannot register as a worker, inject captions or take
part in elections. Without the secret nothing changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any


MESSAGE_TYPES = {
    "register", "register_ack", "transcript_chunk", "translated_chunk",
    "heartbeat", "heartbeat_ack", "election", "election_ok",
    "leader_announce", "discover_worker", "worker_info", "error", "chunk_status", "replicate_batch", "chunk_ack",
    "subscribe", "vote_request", "vote_reply", "leader_heartbeat",
}

AUTH_WINDOW_SECONDS = 120          # a proof older or newer than this is refused (clocks may differ a little)
_SEEN_LIMIT = 20000


def cluster_secret() -> str:
    """Read each time, so it can be set or changed without reloading this module."""
    return os.getenv("POLYGLOT_CLUSTER_SECRET") or ""


def _mac(secret: str, stamp: int, nonce: str, kind: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{stamp}:{nonce}:{kind}".encode("utf-8"), hashlib.sha256).hexdigest()


def sign(msg: dict[str, Any], secret: str | None = None) -> dict[str, Any]:
    secret = cluster_secret() if secret is None else secret
    if not secret:
        return msg
    stamp, nonce = int(time.time()), secrets.token_hex(8)
    return {**msg, "auth": {"ts": stamp, "nonce": nonce, "mac": _mac(secret, stamp, nonce, str(msg.get("type")))}}


_seen: OrderedDict[str, float] = OrderedDict()
_seen_lock = threading.Lock()


def authenticate(msg: dict[str, Any], secret: str | None = None, now: float | None = None) -> bool:
    """True when no secret is configured, or `msg` carries a valid proof that has not been used before."""
    secret = cluster_secret() if secret is None else secret
    if not secret:
        return True
    proof = msg.get("auth")
    if not isinstance(proof, dict):
        return False
    try:
        stamp, nonce, mac = int(proof["ts"]), str(proof["nonce"]), str(proof["mac"])
    except (KeyError, TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    if abs(now - stamp) > AUTH_WINDOW_SECONDS or not nonce:
        return False
    if not hmac.compare_digest(mac, _mac(secret, stamp, nonce, str(msg.get("type")))):
        return False
    with _seen_lock:
        while _seen and (len(_seen) > _SEEN_LIMIT or next(iter(_seen.values())) < now - 2 * AUTH_WINDOW_SECONDS):
            _seen.popitem(last=False)
        if nonce in _seen:
            return False                    # a recording of an earlier message, played back
        _seen[nonce] = now
    return True


class ProtocolError(ValueError):
    """Raised when a malformed protocol frame is received."""


def encode(msg: dict[str, Any]) -> bytes:
    """Serialize one message as compact UTF-8 JSON followed by a newline."""
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("message must be a dict containing a string 'type'")
    if msg["type"] not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type: {msg['type']}")
    return (json.dumps(sign(msg), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_stream(sock: socket.socket) -> Iterator[dict[str, Any]]:
    """Yield complete messages from a socket until the peer disconnects."""
    buffer = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            if buffer.strip():
                raise ProtocolError("connection closed with an incomplete JSON frame")
            return
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"invalid JSON frame: {exc}") from exc
            if not isinstance(message, dict) or message.get("type") not in MESSAGE_TYPES:
                raise ProtocolError("decoded frame has an invalid message type")
            yield message


def recv_one(sock: socket.socket) -> dict[str, Any]:
    """Receive one frame without consuming bytes from a following frame."""
    # Peek, then consume exactly up to the end of this frame. Reading one byte at a time made a 300 KB
    # reply (the broker's job table) take ~1.6s - 160x slower than one bulk read - and the dashboard
    # asks for it every second. Peeking keeps the guarantee that bytes of a following frame stay unread.
    data = bytearray()
    while True:
        peeked = sock.recv(65536, socket.MSG_PEEK)
        if not peeked:
            raise ConnectionError("peer closed before sending a response")
        newline = peeked.find(b"\n")
        take = newline + 1 if newline >= 0 else len(peeked)
        chunk = sock.recv(take)
        if not chunk:
            raise ConnectionError("peer closed before sending a response")
        if chunk.endswith(b"\n"):
            data.extend(chunk[:-1])
            break
        data.extend(chunk)
        if len(data) > 1_000_000:
            raise ProtocolError("JSON frame exceeds 1 MB")
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(message, dict) or message.get("type") not in MESSAGE_TYPES:
        raise ProtocolError("decoded frame has an invalid message type")
    return message
