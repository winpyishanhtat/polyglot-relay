"""Thread-safe state machine for the Bully leader-election algorithm."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable


class BullyElection:
    """Coordinate elections while transport remains the broker's concern."""

    def __init__(self, broker_id: int, peer_ids: list[int],
                 request: Callable[[int, dict], dict | None],
                 announce: Callable[[int], None],
                 on_leader_change: Callable[[int], None],
                 election_timeout: float,
                 logger: logging.Logger | None = None) -> None:
        self.broker_id = broker_id
        self.peer_ids = sorted(peer_ids)
        self.request = request
        self.announce = announce
        self.on_leader_change = on_leader_change
        self.election_timeout = election_timeout
        self.log = logger or logging.getLogger(__name__)
        self.leader_id: int | None = None
        self._lock = threading.RLock()
        self._election_lock = threading.Lock()
        self._announcement = threading.Event()

    def receive_announcement(self, leader_id: int) -> None:
        with self._lock:
            self.leader_id = leader_id
            self._announcement.set()
        self.on_leader_change(leader_id)

    def receive_election(self, candidate_id: int) -> None:
        if self.broker_id > candidate_id:
            threading.Thread(target=self.start, name=f"election-{self.broker_id}", daemon=True).start()

    def start(self) -> int | None:
        """Run one election attempt and return the known leader, if any."""
        if not self._election_lock.acquire(blocking=False):
            return self.leader_id
        retry = False
        try:
            started = time.monotonic()
            self._announcement.clear()
            higher = [peer_id for peer_id in self.peer_ids if peer_id > self.broker_id]
            self.log.info("Broker %s starting election; probing higher IDs %s", self.broker_id, higher)
            got_ok = False
            for peer_id in higher:
                response = self.request(peer_id, {"type": "election", "candidate_id": self.broker_id})
                if response and response.get("type") == "election_ok":
                    got_ok = True
            if not got_ok:
                with self._lock:
                    self.leader_id = self.broker_id
                self.on_leader_change(self.broker_id)
                self.announce(self.broker_id)
                self.log.info("Broker %s elected leader in %.3fs", self.broker_id, time.monotonic() - started)
                return self.broker_id
            if self._announcement.wait(self.election_timeout * 2):
                return self.leader_id
            self.log.warning("No leader announcement arrived; restarting election")
            retry = True
        finally:
            self._election_lock.release()
        if retry:
            time.sleep(min(0.1 * self.broker_id, 0.5))
            return self.start()
        return self.leader_id
