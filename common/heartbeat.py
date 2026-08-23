"""Small reusable heartbeat helpers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class HeartbeatSender:
    def __init__(self, send: Callable[[], None], interval: float) -> None:
        self._send = send
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="heartbeat-sender", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._send()
            except (OSError, ConnectionError):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)


class FailureDetector:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.last_seen = time.monotonic()
        self._lock = threading.Lock()

    def seen(self) -> None:
        with self._lock:
            self.last_seen = time.monotonic()

    def expired(self) -> bool:
        with self._lock:
            return time.monotonic() - self.last_seen > self.timeout

