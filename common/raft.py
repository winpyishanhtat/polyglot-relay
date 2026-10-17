"""Raft-style leader election: terms, one vote per term, and a leader only with a majority.

Chosen with POLYGLOT_ELECTION=raft (the default stays Bully). The differences that matter:

* A broker becomes leader only with votes from a majority of all brokers (2 of 3, 3 of 5), each broker voting
  at most once per term. Two leaders can therefore never be elected in the same term, and a broker cut off
  in a minority can never be elected at all - no split brain, even when the network splits.
* A leader that cannot reach a majority for HEARTBEAT_TIMEOUT steps down and stops taking work, so the
  minority side of a split goes quiet instead of translating into the void.
* Every message carries the term; anything from an older term is refused, and the leader's term goes into
  the jobs it replicates, so a standby ignores a deposed leader.
* The term and the vote are kept on disk (POLYGLOT_RAFT_STATE) so a broker that restarts cannot vote twice.

The price is availability: with 3 brokers the relay survives one failure, not two (use 5 for two). Only the
election is Raft; jobs are still replicated as before, not through a Raft log.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable


class RaftElection:
    mode = "raft"

    def __init__(self, broker_id: int, peer_ids: list[int],
                 request: Callable[[int, dict], dict | None],
                 on_leader_change: Callable[[int | None], None],
                 heartbeat_timeout: float,
                 state_path: str | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.broker_id = broker_id
        self.peers = sorted(p for p in peer_ids if p != broker_id)
        self.cluster_size = len(self.peers) + 1
        self.majority = self.cluster_size // 2 + 1
        self.request = request
        self.on_leader_change = on_leader_change
        self.heartbeat_timeout = heartbeat_timeout
        self.state_path = state_path
        self.log = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._election_lock = threading.Lock()
        self.term = 0
        self.voted_for: int | None = None
        self.role = "follower"
        self._leader_id: int | None = None
        self._last_heard = time.monotonic()
        self._last_majority = time.monotonic()
        self._timeout = self._new_timeout()
        self._load()

    # ---- persistent state ----------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.term = int(saved.get("term", 0))
            self.voted_for = saved.get("voted_for")
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            directory = os.path.dirname(self.state_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temporary = self.state_path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"term": self.term, "voted_for": self.voted_for}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except OSError as exc:
            self.log.warning("Could not save the election state to %s: %s", self.state_path, exc)

    # ---- helpers -------------------------------------------------------------------------------

    def _new_timeout(self) -> float:
        # Randomised so that brokers rarely time out together and split the vote.
        return self.heartbeat_timeout * random.uniform(1.0, 2.0)

    @property
    def leader_id(self) -> int | None:
        return self._leader_id

    @leader_id.setter
    def leader_id(self, value: int | None) -> None:        # tests and tools set it directly, as with Bully
        with self._lock:
            self._leader_id = value
            self.role = "leader" if value == self.broker_id else "follower"

    def _set_leader(self, leader: int | None) -> None:
        """Caller holds the lock. Reports a change outside it."""
        changed = leader != self._leader_id
        self._leader_id = leader
        return changed

    def _step_down(self, term: int) -> bool:
        """Caller holds the lock: a newer term was seen, so this broker is a follower in it."""
        if term > self.term:
            self.term, self.voted_for = term, None
            self._save()
        self.role = "follower"
        return self._set_leader(None if self._leader_id == self.broker_id else self._leader_id)

    def _ask_all(self, message: dict) -> list[dict]:
        replies: list[dict] = []
        lock = threading.Lock()

        def ask(peer: int) -> None:
            reply = self.request(peer, message)
            if reply:
                with lock:
                    replies.append(reply)
        threads = [threading.Thread(target=ask, args=(peer,), daemon=True) for peer in self.peers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return replies

    # ---- the election --------------------------------------------------------------------------

    def heartbeat_expired(self) -> bool:
        with self._lock:
            return self.role != "leader" and time.monotonic() - self._last_heard > self._timeout

    def start(self) -> int | None:
        """Stand for election in a new term. Returns the leader afterwards, if any."""
        if not self._election_lock.acquire(blocking=False):
            return self._leader_id
        try:
            with self._lock:
                self.term += 1
                self.voted_for = self.broker_id
                self.role = "candidate"
                term = self.term
                self._last_heard = time.monotonic()
                self._timeout = self._new_timeout()
                changed = self._set_leader(None)
                self._save()
            if changed:
                self.on_leader_change(None)
            self.log.info("Broker %s standing for election in term %s", self.broker_id, term)
            started = time.monotonic()
            votes = 1
            newer = 0
            for reply in self._ask_all({"type": "vote_request", "term": term, "candidate_id": self.broker_id}):
                if int(reply.get("term", 0)) > term:
                    newer = max(newer, int(reply["term"]))
                elif reply.get("granted"):
                    votes += 1
            with self._lock:
                if newer:
                    changed = self._step_down(newer)
                    won = False
                else:
                    won = self.role == "candidate" and self.term == term and votes >= self.majority
                    if won:
                        self.role = "leader"
                        self._last_majority = time.monotonic()
                        changed = self._set_leader(self.broker_id)
                    elif self.role == "candidate":
                        self.role = "follower"       # lost or split: wait for a leader or the next timeout
            if won:
                self.log.info("Broker %s elected leader for term %s with %s of %s votes in %.3fs",
                              self.broker_id, term, votes, self.cluster_size, time.monotonic() - started)
                if changed:
                    self.on_leader_change(self.broker_id)
                self.leader_tick()
            else:
                self.log.info("Broker %s not elected in term %s (%s of %s votes, %s needed)",
                              self.broker_id, term, votes, self.cluster_size, self.majority)
            return self._leader_id
        finally:
            self._election_lock.release()

    def handle_vote_request(self, message: dict) -> dict:
        term, candidate = int(message.get("term", 0)), message.get("candidate_id")
        changed = False
        with self._lock:
            if term > self.term:
                changed = self._step_down(term)
            granted = term == self.term and self.voted_for in (None, candidate) and self.role != "leader"
            if granted:
                self.voted_for = candidate
                self._save()
                self._last_heard = time.monotonic()     # give the candidate time to win before standing ourselves
            reply = {"type": "vote_reply", "term": self.term, "granted": granted, "sender_id": self.broker_id}
        if changed:
            self.on_leader_change(self._leader_id)
        return reply

    def handle_heartbeat(self, message: dict) -> dict:
        term, leader = int(message.get("term", 0)), message.get("leader_id")
        changed = False
        with self._lock:
            if term < self.term:
                return {"type": "heartbeat_ack", "term": self.term, "success": False, "sender_id": self.broker_id,
                        "leader_id": self._leader_id}
            if term > self.term:
                self.term, self.voted_for = term, None
                self._save()
            self.role = "follower"
            changed = self._set_leader(leader)
            self._last_heard = time.monotonic()
            self._timeout = self._new_timeout()
            reply = {"type": "heartbeat_ack", "term": self.term, "success": True, "sender_id": self.broker_id,
                     "leader_id": leader}
        if changed:
            self.on_leader_change(leader)
        return reply

    def leader_tick(self) -> None:
        """Called by the leader every heartbeat interval: assert leadership, and step down without a majority."""
        with self._lock:
            if self.role != "leader":
                return
            term = self.term
        replies = self._ask_all({"type": "leader_heartbeat", "term": term, "leader_id": self.broker_id})
        acks = 1 + sum(1 for r in replies if r.get("success") and int(r.get("term", 0)) == term)
        newer = max([int(r.get("term", 0)) for r in replies] + [0])
        changed = False
        with self._lock:
            if self.term != term or self.role != "leader":
                return
            if newer > term:
                self.log.warning("Broker %s saw a newer term %s; stepping down", self.broker_id, newer)
                changed = self._step_down(newer)
            elif acks >= self.majority:
                self._last_majority = time.monotonic()
            elif time.monotonic() - self._last_majority > self.heartbeat_timeout:
                self.log.warning("Broker %s reached only %s of %s brokers for %.1fs; stepping down so a minority "
                                 "cannot lead", self.broker_id, acks, self.cluster_size, self.heartbeat_timeout)
                self.role = "follower"
                self._last_heard = time.monotonic()
                changed = self._set_leader(None)
        if changed:
            self.on_leader_change(self._leader_id)

    # ---- Bully messages from brokers that run the other algorithm are ignored ----------------------

    def receive_election(self, candidate_id: int) -> None:
        return None

    def receive_announcement(self, leader_id: int) -> None:
        return None
