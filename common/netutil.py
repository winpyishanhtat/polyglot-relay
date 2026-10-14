"""TCP connections that never wait on a DNS lookup for a host we have reached before.

socket.create_connection's timeout covers the connect, not the DNS lookup. In Docker, resolving the
name of a stopped container takes about 4 seconds, so every walk down the broker list (the dashboard's
status check, a worker finding the new leader, an election probing its peers) paid 4 s for each dead
broker, and the dashboard froze while any broker was down.

The rules, in order of importance:

* A host resolved once keeps its last known address. A stopped container's name stops resolving, but its
  address is still the right one to try: the connection is refused at once instead of stalling, and when
  the container restarts (Docker normally gives it the same address) the very next attempt succeeds.
  Never treat "the name did not resolve" as "the peer is down" for a host that was reachable before: a
  false "down" during an election makes two brokers both claim leadership.
* The address is re-checked in the background, so a changed address is picked up without anyone waiting.
* Only the very first lookup of a host waits (at most `timeout`), and a failure of that lookup is
  remembered for a moment so a burst of probes does not stall repeatedly.
* Numeric addresses skip all of it.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time

REFRESH_AFTER = 15.0          # re-check a known address in the background after this long
REFRESH_BACKOFF = 2.0         # but never more often than this per host (less than a heartbeat timeout)
NEGATIVE_TTL = 1.0            # after a failed *first* lookup, fail instantly for this long

_lock = threading.Lock()
_known: dict[str, tuple[str, float]] = {}        # host -> (address, when it was last confirmed)
_failed: dict[str, float] = {}                   # host -> until when to fail instantly (first lookups only)
_refresh_attempted: dict[str, float] = {}        # host -> when a background refresh last started
_refreshing: set[str] = set()


def clear_cache() -> None:
    with _lock:
        _known.clear()
        _failed.clear()
        _refresh_attempted.clear()
        _refreshing.clear()


def _is_address(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _lookup(host: str) -> str:
    return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0]


def _refresh_in_background(host: str) -> None:
    """Re-resolve host without making anyone wait; keep the old address if the lookup fails."""
    now = time.monotonic()
    with _lock:
        if host in _refreshing or now - _refresh_attempted.get(host, 0.0) < REFRESH_BACKOFF:
            return
        _refreshing.add(host)
        _refresh_attempted[host] = now

    def work() -> None:
        try:
            address = _lookup(host)
        except OSError:
            address = None
        with _lock:
            _refreshing.discard(host)
            if address:
                _known[host] = (address, time.monotonic())

    threading.Thread(target=work, name=f"refresh-{host}", daemon=True).start()


def resolve(host: str, timeout: float) -> str:
    """The address for host. After the first success this never waits."""
    if _is_address(host):
        return host
    now = time.monotonic()
    with _lock:
        known = _known.get(host)
        failed_until = _failed.get(host, 0.0)
    if known:
        if now - known[1] > REFRESH_AFTER:
            _refresh_in_background(host)
        return known[0]
    if failed_until > now:
        raise ConnectionError(f"{host} did not resolve a moment ago")
    outcome: dict[str, object] = {}

    def lookup() -> None:
        try:
            outcome["address"] = _lookup(host)
        except OSError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=lookup, name=f"resolve-{host}", daemon=True)
    worker.start()
    worker.join(timeout)
    if "address" in outcome:
        with _lock:
            _known[host] = (str(outcome["address"]), time.monotonic())
            _failed.pop(host, None)
        return str(outcome["address"])
    with _lock:
        _failed[host] = time.monotonic() + NEGATIVE_TTL
    error = outcome.get("error")
    raise error if isinstance(error, OSError) else TimeoutError(f"looking up {host} took longer than {timeout}s")


def connect(host: str, port: int, timeout: float) -> socket.socket:
    """Like socket.create_connection((host, port), timeout), but a lookup never stalls the caller after the
    first success, and a refused connection to a known address fails at once."""
    address = resolve(host, timeout)
    try:
        return socket.create_connection((address, port), timeout)
    except OSError:
        if not _is_address(host):
            _refresh_in_background(host)     # maybe the container came back at a different address
        raise
