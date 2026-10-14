"""TCP connections whose name lookup has a deadline.

socket.create_connection's timeout covers the connect, not the DNS lookup. In Docker, resolving the
name of a stopped container takes about 4 seconds, so every walk down the broker list (the dashboard's
status check, a worker finding the new leader, an election probing its peers) paid 4 s for each dead
broker, and the dashboard froze while any broker was down.

connect() resolves with a deadline, remembers a failed lookup briefly (so the next probe fails at once
instead of stalling again) and remembers a good one a little longer. Numeric addresses skip all of it.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time

NEGATIVE_TTL = 5.0     # after a failed lookup, fail instantly for this long
POSITIVE_TTL = 15.0    # a good answer is reused for this long (container IPs rarely change)

_lock = threading.Lock()
_negative: dict[str, float] = {}
_positive: dict[str, tuple[str, float]] = {}


def clear_cache() -> None:
    with _lock:
        _negative.clear()
        _positive.clear()


def _is_address(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve(host: str, timeout: float) -> str:
    """The address for host, waiting at most `timeout` seconds for the lookup."""
    if _is_address(host):
        return host
    now = time.monotonic()
    with _lock:
        if _negative.get(host, 0.0) > now:
            raise ConnectionError(f"{host} did not resolve a moment ago")
        known = _positive.get(host)
        if known and known[1] > now:
            return known[0]
    outcome: dict[str, object] = {}

    def lookup() -> None:
        try:
            outcome["address"] = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0]
        except OSError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=lookup, name=f"resolve-{host}", daemon=True)
    worker.start()
    worker.join(timeout)
    now = time.monotonic()
    if "address" in outcome:
        with _lock:
            _positive[host] = (str(outcome["address"]), now + POSITIVE_TTL)
            _negative.pop(host, None)
        return str(outcome["address"])
    with _lock:
        _negative[host] = now + NEGATIVE_TTL
    error = outcome.get("error")
    raise error if isinstance(error, OSError) else TimeoutError(f"looking up {host} took longer than {timeout}s")


def connect(host: str, port: int, timeout: float) -> socket.socket:
    """Like socket.create_connection((host, port), timeout), but a slow or failing name lookup costs at
    most `timeout` once, then nothing for a few seconds."""
    address = resolve(host, timeout)
    try:
        return socket.create_connection((address, port), timeout)
    except OSError:
        with _lock:
            _positive.pop(host, None)   # the address may be stale (the container came back elsewhere)
        raise
