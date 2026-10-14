"""Newline-delimited JSON wire protocol."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from typing import Any


MESSAGE_TYPES = {
    "register", "register_ack", "transcript_chunk", "translated_chunk",
    "heartbeat", "heartbeat_ack", "election", "election_ok",
    "leader_announce", "discover_worker", "worker_info", "error", "chunk_status", "replicate_batch", "chunk_ack",
}


class ProtocolError(ValueError):
    """Raised when a malformed protocol frame is received."""


def encode(msg: dict[str, Any]) -> bytes:
    """Serialize one message as compact UTF-8 JSON followed by a newline."""
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("message must be a dict containing a string 'type'")
    if msg["type"] not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type: {msg['type']}")
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


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
