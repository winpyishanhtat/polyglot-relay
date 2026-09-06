"""Shared sliding-window rate gate for Gemini's free-tier RPM cap.

One caption fans out to every language worker at once, but the free tier's
request budget (configurable, shared across the project/model, not per
worker; five requests/minute is a local default) can't be enforced by each
worker staggering its own request independently: 9 workers each waiting a
fraction of a second still all land in the same 60-second window. This
server is the single source of truth for "is a slot free right now" - every
Gemini-backed worker calls GET /acquire before its Gemini request and blocks
until one is, so a broadcast burst gets spread out over time instead of
most of it bouncing off Gemini's own 429s (see translator_worker.py's
_await_gemini_slot). Not used on GEMINI_TIER=paid - a billing-enabled
project's RPM ceiling already comfortably absorbs a full burst.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("polyglot.rategate")

WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    """Thread-safe: as many threads call acquire() concurrently as there
    are workers waiting on a slot (one ThreadingHTTPServer request each)."""

    def __init__(self, limit: int, window_seconds: float = WINDOW_SECONDS) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("Rate limit and window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._timestamps: collections.deque[float] = collections.deque()
        self._tickets: collections.deque[object] = collections.deque()

    def acquire(self) -> tuple[float, int]:
        """Blocks until a slot is free, then reserves it. Returns
        (seconds waited, slots now in use) - the latter purely for logging."""
        waited = 0.0
        ticket = object()
        with self._lock:
            self._tickets.append(ticket)
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                    self._timestamps.popleft()
                if self._tickets[0] is ticket and len(self._timestamps) < self.limit:
                    self._tickets.popleft()
                    self._timestamps.append(now)
                    return waited, len(self._timestamps)
                sleep_for = min(self.window_seconds, 0.1)
            time.sleep(sleep_for)
            waited += sleep_for


class GateHandler(BaseHTTPRequestHandler):
    limiter: SlidingWindowLimiter

    def do_GET(self) -> None:
        if self.path.startswith("/acquire"):
            waited, in_window = self.limiter.acquire()
            if waited > 0.5:
                LOG.info("Released a slot after %.1fs wait (%d/%d in window)",
                         waited, in_window, self.limiter.limit)
            self.send_response(200)
            self.end_headers()
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        LOG.debug("HTTP %s", format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--limit-per-minute", type=int,
                         default=int(os.getenv("GEMINI_RATE_LIMIT_PER_MINUTE", "5")))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    GateHandler.limiter = SlidingWindowLimiter(args.limit_per_minute)
    server = ThreadingHTTPServer((args.host, args.port), GateHandler)
    LOG.info("Gemini rate gate ready at http://%s:%s (limit=%s/min)",
             args.host, args.port, args.limit_per_minute)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
