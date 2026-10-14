"""Replicated Polyglot Relay broker with Bully election and worker pools.

Delivery is at-least-once. The leader tracks every (chunk, language) job it hands to a worker, and
redelivers it to another worker when the first one disconnects, fails it, or sits on it too long; a job
for a language with no worker connected waits (bounded) and is delivered when one registers. In-flight work is
also replicated, in small batches, to the standby brokers, so a new leader can replay what the old one was
holding. Workers and the dashboard ignore a chunk they have already handled, so a duplicate is harmless.
Work is balanced by outstanding jobs per worker (ties in round-robin order), not by rotation alone.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from common import config
from common import netutil
from common.election import BullyElection
from common.heartbeat import FailureDetector
from common.protocol import ProtocolError, decode_stream, encode, recv_one


LOG = logging.getLogger("polyglot.broker")

# A job in flight this long (worker connected but silent) is delivered to another worker.
JOB_TIMEOUT = float(os.getenv("POLYGLOT_JOB_TIMEOUT") or "90")
# Deliveries per job (the first plus redeliveries).
MAX_ATTEMPTS = max(1, int(os.getenv("POLYGLOT_MAX_DELIVERY_ATTEMPTS") or "3"))
# A job for a language with no worker connected waits this long for one before it is given up.
WAITING_MAX_AGE = float(os.getenv("POLYGLOT_WAITING_MAX_AGE") or "60")
WAITING_MAX_PER_LANGUAGE = 500
# How often the leader sends its in-flight work to the standby brokers, and how much a standby keeps.
REPLICATION_INTERVAL = float(os.getenv("POLYGLOT_REPLICATION_INTERVAL") or "0.5")
REPLICA_MAX_JOBS = 3000
REPLICA_MAX_AGE = 180.0
# After taking over, wait this long for workers to re-register before replaying the replicated work.
REPLAY_DELAY = float(os.getenv("POLYGLOT_REPLAY_DELAY") or "1.0")
MAX_JOBS_REMEMBERED = 1800


@dataclass
class WorkerConnection:
    worker_id: str
    lang: str
    host: str
    port: int
    sock: socket.socket
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    mode: str = "unknown"
    # chunk_id -> the chunk, for every job sent to this worker and not yet completed
    assigned: dict[str, dict] = field(default_factory=dict)

    def send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            self.sock.sendall(encode(message))


class WorkerRegistry:
    """Thread-safe dynamic registry. choose() returns the worker with the fewest outstanding jobs,
    breaking ties in round-robin order so idle workers share work evenly."""

    def __init__(self) -> None:
        self._workers: dict[str, list[WorkerConnection]] = {lang: [] for lang in config.TARGET_LANGUAGES}
        self._cursor: dict[str, int] = {lang: 0 for lang in config.TARGET_LANGUAGES}
        self._lock = threading.RLock()

    def add(self, worker: WorkerConnection) -> None:
        with self._lock:
            self.remove(worker.worker_id, close=False)
            self._workers[worker.lang].append(worker)

    def remove(self, worker_id: str, close: bool = True) -> WorkerConnection | None:
        with self._lock:
            for lang, pool in self._workers.items():
                for index, worker in enumerate(pool):
                    if worker.worker_id == worker_id:
                        removed = pool.pop(index)
                        if pool:
                            self._cursor[lang] %= len(pool)
                        else:
                            self._cursor[lang] = 0
                        if close:
                            try:
                                removed.sock.close()
                            except OSError:
                                pass
                        return removed
        return None

    def choose(self, lang: str, exclude: set[str] | frozenset[str] = frozenset()) -> WorkerConnection | None:
        with self._lock:
            pool = self._workers.get(lang, [])
            candidates = [w for w in pool if w.worker_id not in exclude]
            if not candidates:
                return None
            start = self._cursor[lang] % len(pool)
            ordered = [w for w in pool[start:] + pool[:start] if w.worker_id not in exclude]
            best = min(ordered, key=lambda w: len(w.assigned))      # first minimum: round-robin among equals
            self._cursor[lang] = (pool.index(best) + 1) % len(pool)
            return best

    def snapshot(self, lang: str) -> list[WorkerConnection]:
        with self._lock:
            return list(self._workers.get(lang, []))

    def all(self) -> list[WorkerConnection]:
        with self._lock:
            return [worker for pool in self._workers.values() for worker in pool]

    def clear(self) -> None:
        for worker in self.all():
            self.remove(worker.worker_id)


class Broker:
    def __init__(self, broker_id: int) -> None:
        self.details = config.broker_by_id(broker_id)
        self.broker_id = broker_id
        self.peers = {b["id"]: b for b in config.BROKERS if b["id"] != broker_id}
        self.registry = WorkerRegistry()
        self.jobs = OrderedDict()
        self._jobs_lock = threading.RLock()
        # (chunk_id, lang) -> {"chunk", "worker_id", "attempts", "sent_at", "excluded"} for work in flight
        self.pending: dict[tuple[str, str], dict] = {}
        # lang -> jobs waiting for a worker of that language to connect
        self.waiting: dict[str, deque] = {lang: deque() for lang in config.TARGET_LANGUAGES}
        # Standby copy of the leader's in-flight work: (chunk_id, lang) -> {"chunk", "at"}
        self.replica: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._replication_lock = threading.Lock()
        self._replicate_adds: dict[str, dict] = {}
        self._replicate_done: list[list[str]] = []
        self.stop_event = threading.Event()
        self.server: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self.election = BullyElection(
            broker_id=broker_id,
            peer_ids=[b["id"] for b in config.BROKERS],
            request=self._peer_request,
            announce=self._announce,
            on_leader_change=self._leader_changed,
            election_timeout=config.ELECTION_TIMEOUT,
            logger=LOG,
        )

    @property
    def is_leader(self) -> bool:
        return self.election.leader_id == self.broker_id

    def _leader_changed(self, leader_id: int) -> None:
        with self._clients_lock:
            was_serving = bool(self._clients) or bool(self.registry.all())
        LOG.info("Leader is broker %s%s", leader_id, " (this replica)" if self.is_leader else "")
        if not self.is_leader:
            with self._jobs_lock:       # only the leader owns in-flight work
                self.pending.clear()
                for queue in self.waiting.values():
                    queue.clear()
            if was_serving:
                # Workers AND producers: a producer left attached to a broker that no longer leads would
                # keep feeding it chunks that no worker will ever see.
                LOG.info("Demoted; disconnecting clients so they can discover the new leader")
                self._disconnect_clients()
        elif self.replica:
            threading.Thread(target=self._replay_replica, name="replay-replica", daemon=True).start()

    def _peer_request(self, peer_id: int, message: dict) -> dict | None:
        peer = self.peers.get(peer_id)
        if not peer:
            return None
        try:
            with netutil.connect(peer["host"], peer["port"], config.CONNECT_TIMEOUT) as sock:
                sock.settimeout(config.CONNECT_TIMEOUT)
                sock.sendall(encode(message))
                return recv_one(sock)
        except (OSError, ConnectionError, ProtocolError):
            return None

    def _announce(self, leader_id: int) -> None:
        for peer_id in self.peers:
            self._peer_request(peer_id, {"type": "leader_announce", "leader_id": leader_id})

    def _redirect(self) -> dict:
        leader_id = self.election.leader_id
        leader = None
        if leader_id is not None:
            try:
                leader = config.broker_by_id(leader_id)
            except ValueError:
                pass
        return {"type": "register_ack", "accepted": False, "leader": leader}

    def serve_forever(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_host = config.BROKER_BIND_HOST or self.details["host"]
        self.server.bind((bind_host, self.details["port"]))
        self.server.listen()
        self.server.settimeout(1)
        LOG.info("Broker %s listening on %s:%s", self.broker_id, bind_host, self.details["port"])
        threading.Thread(target=self._leader_monitor, name="leader-monitor", daemon=True).start()
        threading.Thread(target=self._worker_reaper, name="worker-reaper", daemon=True).start()
        threading.Thread(target=self._replicator, name="replicator", daemon=True).start()
        threading.Thread(target=self._delayed_election, name="startup-election", daemon=True).start()
        while not self.stop_event.is_set():
            try:
                conn, address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_connection, args=(conn, address), daemon=True).start()
        self.shutdown()

    def _delayed_election(self) -> None:
        time.sleep(0.25)
        if not self.stop_event.is_set():
            self.election.start()

    def _handle_connection(self, conn: socket.socket, address: tuple[str, int]) -> None:
        worker: WorkerConnection | None = None
        try:
            messages = decode_stream(conn)
            first = next(messages)
            kind = first["type"]
            if kind == "election":
                candidate_id = int(first["candidate_id"])
                conn.sendall(encode({"type": "election_ok", "responder_id": self.broker_id}))
                self.election.receive_election(candidate_id)
                return
            if kind == "leader_announce":
                self.election.receive_announcement(int(first["leader_id"]))
                conn.sendall(encode({"type": "heartbeat_ack", "sender_id": self.broker_id, "leader_id": self.election.leader_id}))
                return
            if kind == "replicate_batch":
                self._apply_replication(first)
                conn.sendall(encode({"type": "heartbeat_ack", "sender_id": self.broker_id, "leader_id": self.election.leader_id}))
                return
            if kind == "heartbeat" and first.get("role") == "broker":
                conn.sendall(encode({"type": "heartbeat_ack", "sender_id": self.broker_id,
                                     "leader_id": self.election.leader_id, "is_leader": self.is_leader,
                                     **(self.telemetry() if first.get("sender_id") == "dashboard" else {})}))
                return
            if not self.is_leader:
                conn.sendall(encode(self._redirect()))
                return
            if kind == "discover_worker":
                self._handle_discovery(conn, first)
                return
            if kind != "register":
                conn.sendall(encode({"type": "error", "message": "first message must register"}))
                return

            role = first.get("role")
            if role == "worker":
                worker = self._register_worker(conn, first)
                self._track_client(conn)
                self._flush_waiting(worker.lang)
                for message in messages:
                    worker.last_seen = time.monotonic()
                    if message["type"] == "translated_chunk":
                        self._on_translated(worker, message)
                    elif message["type"] == "chunk_status":
                        self._on_status(worker, message)
            elif role == "stt":
                conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": self.broker_id}))
                self._track_client(conn)
                LOG.info("STT producer registered from %s:%s", *address)
                for message in messages:
                    if message["type"] == "transcript_chunk":
                        if not self.is_leader:
                            LOG.info("No longer the leader; dropping the producer so it finds the new one")
                            break
                        self.route_chunk(message)
                        # The chunk is routed and mirrored to the standbys: tell the producer it can forget it.
                        # A producer that never gets this (this broker died first) sends it again.
                        self._replicate_flush()
                        if message.get("ack_id"):
                            conn.sendall(encode({"type": "chunk_ack", "chunk_id": message.get("chunk_id"),
                                                 "ack_id": message["ack_id"]}))
            else:
                conn.sendall(encode({"type": "error", "message": "unknown registration role"}))
        except (StopIteration, ConnectionError, OSError, ProtocolError, ValueError) as exc:
            LOG.debug("Connection %s ended: %s", address, exc)
        finally:
            if worker:
                removed = self.registry.remove(worker.worker_id, close=False)
                if removed:
                    LOG.info("Worker deregistered: %s (%s)", worker.worker_id, worker.lang)
                    self._on_worker_gone(removed)
            self._untrack_client(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _register_worker(self, conn: socket.socket, message: dict) -> WorkerConnection:
        lang = str(message.get("lang"))
        if lang not in config.TARGET_LANGUAGES:
            raise ValueError(f"unsupported language {lang}")
        worker = WorkerConnection(str(message["worker_id"]), lang, str(message["host"]), int(message["port"]), conn)
        worker.mode = str(message.get("mode", "unknown"))
        self.registry.add(worker)
        worker.send({"type": "register_ack", "accepted": True, "leader_id": self.broker_id})
        LOG.info("Worker registered: %s lang=%s endpoint=%s:%s", worker.worker_id, lang, worker.host, worker.port)
        return worker

    def _handle_discovery(self, conn: socket.socket, message: dict) -> None:
        lang = str(message.get("lang"))
        worker = self.registry.choose(lang)
        if worker:
            response = {"type": "worker_info", "lang": lang, "worker_id": worker.worker_id,
                        "host": worker.host, "port": worker.port}
        else:
            response = {"type": "worker_info", "lang": lang, "worker_id": None}
        conn.sendall(encode(response))

    # ---- routing and delivery ------------------------------------------------------------

    def route_chunk(self, chunk: dict) -> None:
        # Optional "targets": route only to these languages. A producer uses it
        # to send one chunk in two stages (see web_ui's English pivot); a chunk
        # without it goes to every language, exactly as before.
        targets = chunk.get("targets")
        for lang in config.TARGET_LANGUAGES:
            if targets is not None and lang not in targets:
                continue
            if lang == chunk.get("source_lang"):
                result = {**chunk, "type": "translated_chunk", "lang": lang,
                          "worker_id": f"broker-{self.broker_id}", "translation_backend": "passthrough"}
                self.update_job(result, "source")
                self._publish_translation(result)
                continue
            if self._already_handled(chunk.get("chunk_id"), lang):
                LOG.info("Ignoring a repeated chunk %s for %s", chunk.get("chunk_id"), lang)
                continue
            self._dispatch(chunk, lang, attempts=1, excluded=frozenset(), replicate=True)

    def _already_handled(self, chunk_id: Any, lang: str) -> bool:
        """A producer that reconnects mid-send repeats the chunk; do not translate it twice."""
        key = (chunk_id, lang)
        with self._jobs_lock:
            if key in self.pending:
                return True
            row = self.jobs.get(key)
            return bool(row and row["status"] in ("completed", "source"))

    def _dispatch(self, chunk: dict, lang: str, attempts: int, excluded: frozenset[str] | set[str],
                  replicate: bool = False, detail: str | None = None) -> bool:
        """Hand one job to the least-loaded worker of the language. Returns False when it had to wait
        (or could not be delivered). Redeliveries pass the workers that already failed it as `excluded`."""
        key = (chunk.get("chunk_id"), lang)
        while True:
            worker = self.registry.choose(lang, excluded)
            if worker is None:
                break
            try:
                with self._jobs_lock:
                    worker.assigned[chunk["chunk_id"]] = chunk
                    self.pending[key] = {"chunk": chunk, "worker_id": worker.worker_id, "attempts": attempts,
                                         "sent_at": time.monotonic(), "excluded": set(excluded)}
                note = {"detail": detail} if detail else {}
                self.update_job({**chunk, "lang": lang, "worker_id": worker.worker_id, **note}, "queued")
                worker.send(chunk)
                LOG.info("Forwarded chunk %s to %s worker %s (attempt %s)", chunk.get("chunk_id"), lang, worker.worker_id, attempts)
                if replicate:
                    self._replicate_add(chunk, lang)
                return True
            except OSError:
                with self._jobs_lock:
                    worker.assigned.pop(chunk["chunk_id"], None)
                    self.pending.pop(key, None)
                removed = self.registry.remove(worker.worker_id)
                LOG.warning("Removed unreachable worker %s", worker.worker_id)
                if removed:
                    self._on_worker_gone(removed)
        self._wait_for_worker(chunk, lang, attempts, excluded, replicate)
        return False

    def _wait_for_worker(self, chunk: dict, lang: str, attempts: int, excluded, replicate: bool) -> None:
        """No worker can take this job right now: keep it (bounded) and deliver when one connects."""
        has_any_worker = bool(self.registry.snapshot(lang))
        with self._jobs_lock:
            queue = self.waiting[lang]
            if len(queue) >= WAITING_MAX_PER_LANGUAGE:
                queue.popleft()
            if not has_any_worker or not excluded:
                queue.append({"chunk": chunk, "attempts": attempts, "since": time.monotonic(), "excluded": set(excluded)})
            else:
                # Every worker that is connected has already failed it: waiting will not help.
                self.update_job({**chunk, "lang": lang}, "failed")
                self._replicate_finished(chunk.get("chunk_id"), lang)
                return
        self.update_job({**chunk, "lang": lang, "detail": "Waiting for a worker; it will be delivered when one connects"}, "unavailable")
        LOG.warning("No available %s worker for chunk %s", lang, chunk.get("chunk_id"))
        if replicate:
            self._replicate_add(chunk, lang)

    def _flush_waiting(self, lang: str) -> None:
        """A worker for `lang` just connected: deliver what was waiting for one."""
        with self._jobs_lock:
            queue = self.waiting[lang]
            entries = list(queue)
            queue.clear()
        now = time.monotonic()
        for entry in entries:
            if now - entry["since"] > WAITING_MAX_AGE:
                self.update_job({**entry["chunk"], "lang": lang, "detail": "No worker connected in time"}, "unavailable")
                self._replicate_finished(entry["chunk"].get("chunk_id"), lang)
                continue
            self._dispatch(entry["chunk"], lang, entry["attempts"], entry["excluded"],
                           detail="Delivered once a worker connected")

    def _on_translated(self, worker: WorkerConnection, message: dict) -> None:
        key = (message.get("chunk_id"), message.get("lang"))
        with self._jobs_lock:
            entry = self.pending.pop(key, None)
            worker.assigned.pop(message.get("chunk_id"), None)
            row = self.jobs.get(key)
            duplicate = entry is None and bool(row and row["status"] in ("completed", "source"))
        if duplicate:
            LOG.info("Ignoring a repeated result for chunk %s (%s)", key[0], key[1])
            return
        self.update_job(message, "completed")
        self._replicate_finished(key[0], key[1])
        self._publish_translation(message)

    def _on_status(self, worker: WorkerConnection, message: dict) -> None:
        status = str(message.get("status", "processing"))
        key = (message.get("chunk_id"), message.get("lang"))
        if status == "failed":
            with self._jobs_lock:
                entry = self.pending.get(key)
            if entry and entry["worker_id"] != worker.worker_id:
                LOG.info("Ignoring a stale failure report from %s: chunk %s now belongs to %s", worker.worker_id, key[0], entry["worker_id"])
                return
            detail = str(message.get("detail", ""))
            if entry and entry["attempts"] < MAX_ATTEMPTS and "quota" not in detail.lower():
                excluded = set(entry["excluded"]) | {worker.worker_id}
                with self._jobs_lock:
                    worker.assigned.pop(key[0], None)
                    self.pending.pop(key, None)
                if self.registry.choose(key[1], excluded) is not None:
                    self._dispatch(entry["chunk"], key[1], entry["attempts"] + 1, excluded,
                                   detail=f"Retried on another worker after a failure ({worker.worker_id})")
                    return
                with self._jobs_lock:       # nobody else to try: report the failure as it was
                    self.pending[key] = entry
                    worker.assigned[key[0]] = entry["chunk"]
            with self._jobs_lock:
                self.pending.pop(key, None)
                worker.assigned.pop(key[0], None)
            self._replicate_finished(key[0], key[1])
        self.update_job(message, status)

    def _on_worker_gone(self, worker: WorkerConnection) -> None:
        """A worker disconnected (or was dropped): deliver what it was holding to another worker."""
        with self._jobs_lock:
            orphaned = list(worker.assigned.values())
            worker.assigned.clear()
        for chunk in orphaned:
            key = (chunk.get("chunk_id"), worker.lang)
            with self._jobs_lock:
                entry = self.pending.pop(key, None)
            if entry is None:
                continue
            if entry["attempts"] >= MAX_ATTEMPTS:
                self.update_job({**chunk, "lang": worker.lang, "worker_id": worker.worker_id,
                                 "detail": "Worker lost the job and the retry limit was reached"}, "failed")
                self._replicate_finished(key[0], key[1])
                continue
            LOG.info("Redelivering chunk %s (%s) after losing worker %s", key[0], worker.lang, worker.worker_id)
            self._dispatch(chunk, worker.lang, entry["attempts"] + 1, set(entry["excluded"]),
                           detail=f"Redelivered after worker {worker.worker_id} was lost")

    def _redeliver_stuck(self) -> None:
        """A connected worker that has held a job for JOB_TIMEOUT is not going to finish it."""
        now = time.monotonic()
        with self._jobs_lock:
            stuck = [(key, entry) for key, entry in self.pending.items() if now - entry["sent_at"] > JOB_TIMEOUT]
        for key, entry in stuck:
            with self._jobs_lock:
                if self.pending.get(key) is not entry:
                    continue
                self.pending.pop(key)
                for worker in self.registry.snapshot(key[1]):
                    if worker.worker_id == entry["worker_id"]:
                        worker.assigned.pop(key[0], None)
            if entry["attempts"] >= MAX_ATTEMPTS:
                self.update_job({**entry["chunk"], "lang": key[1], "detail": "No result within the time limit"}, "failed")
                self._replicate_finished(key[0], key[1])
                continue
            LOG.warning("Chunk %s (%s) was not finished by %s in %.0fs; redelivering", key[0], key[1], entry["worker_id"], JOB_TIMEOUT)
            self._dispatch(entry["chunk"], key[1], entry["attempts"] + 1, set(entry["excluded"]) | {entry["worker_id"]},
                           detail=f"Redelivered: {entry['worker_id']} did not finish in time")

    def _expire_waiting(self) -> None:
        now = time.monotonic()
        for lang, queue in self.waiting.items():
            with self._jobs_lock:
                expired = [e for e in queue if now - e["since"] > WAITING_MAX_AGE]
                for entry in expired:
                    queue.remove(entry)
            for entry in expired:
                self.update_job({**entry["chunk"], "lang": lang, "detail": "No worker connected in time"}, "unavailable")
                self._replicate_finished(entry["chunk"].get("chunk_id"), lang)

    # ---- replication to the standby brokers -----------------------------------------------

    def _replicate_add(self, chunk: dict, lang: str) -> None:
        if not self.peers:
            return
        slim = {k: v for k, v in chunk.items() if k != "audio_pcm_b64"}     # the text pipeline only; audio is large
        with self._replication_lock:
            entry = self._replicate_adds.setdefault(str(chunk.get("chunk_id")), {"chunk": slim, "langs": []})
            if lang not in entry["langs"]:
                entry["langs"].append(lang)

    def _replicate_finished(self, chunk_id: Any, lang: str) -> None:
        if not self.peers:
            return
        with self._replication_lock:
            entry = self._replicate_adds.get(str(chunk_id))
            if entry and lang in entry["langs"]:
                entry["langs"].remove(lang)          # finished before it was ever sent: nothing to replicate
                if not entry["langs"]:
                    self._replicate_adds.pop(str(chunk_id), None)
                return
            self._replicate_done.append([str(chunk_id), lang])

    def _replicate_flush(self) -> bool:
        """Send what accumulated since the last flush to every other broker. Best effort."""
        with self._replication_lock:
            adds = [{"chunk": e["chunk"], "langs": list(e["langs"])} for e in self._replicate_adds.values() if e["langs"]]
            done = list(self._replicate_done)
            self._replicate_adds.clear()
            self._replicate_done.clear()
        if not adds and not done:
            return False
        for start in range(0, max(len(adds), 1), 100):
            batch = {"type": "replicate_batch", "leader_id": self.broker_id, "adds": adds[start:start + 100],
                     "done": done if start == 0 else []}
            for peer_id in self.peers:
                self._peer_request(peer_id, batch)
        return True

    def _replicator(self) -> None:
        while not self.stop_event.wait(REPLICATION_INTERVAL):
            if self.is_leader:
                try:
                    self._replicate_flush()
                except Exception:  # replication is best effort; it must never take the broker down
                    LOG.exception("Replication flush failed")

    def _apply_replication(self, batch: dict) -> None:
        """A standby records the leader's in-flight work (bounded in count and age)."""
        now = time.time()
        with self._jobs_lock:
            for item in batch.get("adds", []):
                chunk = item.get("chunk") or {}
                for lang in item.get("langs", []):
                    if lang in config.TARGET_LANGUAGES and chunk.get("chunk_id") is not None:
                        self.replica[(str(chunk["chunk_id"]), lang)] = {"chunk": chunk, "at": now}
            for chunk_id, lang in batch.get("done", []):
                self.replica.pop((str(chunk_id), lang), None)
            while len(self.replica) > REPLICA_MAX_JOBS:
                self.replica.popitem(last=False)
            while self.replica and now - next(iter(self.replica.values()))["at"] > REPLICA_MAX_AGE:
                self.replica.popitem(last=False)

    def _replay_replica(self) -> None:
        """This broker just became leader: deliver the work the old leader had not finished."""
        if self.stop_event.wait(REPLAY_DELAY):
            return
        with self._jobs_lock:
            entries = list(self.replica.items())
            self.replica.clear()
        now = time.time()
        replayed = 0
        for (chunk_id, lang), entry in entries:
            if not self.is_leader or now - entry["at"] > REPLICA_MAX_AGE:
                continue
            if self._already_handled(chunk_id, lang):
                continue
            self._dispatch(entry["chunk"], lang, attempts=1, excluded=frozenset(), replicate=True,
                           detail="Recovered after a broker failover")
            replayed += 1
        if replayed:
            LOG.info("Replayed %s jobs that the previous leader had not finished", replayed)

    # ---- job table and telemetry ------------------------------------------------------------

    def update_job(self, message: dict, status: str) -> None:
        key = (message.get("chunk_id"), message.get("lang"))
        with self._jobs_lock:
            row = self.jobs.setdefault(key, {"chunk_id": key[0], "lang": key[1]})
            row.update(status=status, updated_at=time.time())
            for field_name in ("worker_id", "detail"):
                if field_name in message:
                    row[field_name] = message[field_name]
            while len(self.jobs) > MAX_JOBS_REMEMBERED:
                self.jobs.popitem(last=False)

    def telemetry(self) -> dict:
        registered = {w.worker_id for w in self.registry.all()}
        with self._jobs_lock:
            for row in self.jobs.values():
                if row["status"] in ("queued", "processing", "quota_wait", "retrying", "translating") and row.get("worker_id") not in registered:
                    row.update(status="interrupted", detail="Worker disconnected; resubmit this text")
            jobs = [dict(row) for row in self.jobs.values()]
        members = [{"worker_id": w.worker_id, "lang": w.lang,
                    "mode": w.mode, "queue": len(w.assigned),
                    "heartbeat_age": round(time.monotonic() - w.last_seen, 1)}
                   for w in self.registry.all()]
        return {"members": members, "jobs": jobs}

    def _publish_translation(self, message: dict) -> None:
        lang = str(message.get("lang"))
        for worker in self.registry.snapshot(lang):
            try:
                worker.send(message)
            except OSError:
                removed = self.registry.remove(worker.worker_id)
                if removed:
                    self._on_worker_gone(removed)

    def _leader_monitor(self) -> None:
        # Wall-clock detector: a probe to a dead leader can itself block for
        # CONNECT_TIMEOUT, so counting failed polls (one INTERVAL each) made
        # detection take several times HEARTBEAT_TIMEOUT.
        detector = FailureDetector(config.HEARTBEAT_TIMEOUT)
        while not self.stop_event.wait(config.HEARTBEAT_INTERVAL):
            if self.is_leader:
                detector.seen()
                self._yield_to_a_higher_broker()
                continue
            leader_id = self.election.leader_id
            response = self._peer_request(leader_id, {"type": "heartbeat", "sender_id": self.broker_id, "role": "broker"}) if leader_id else None
            if response and response.get("is_leader"):
                detector.seen()
                continue
            if detector.expired():
                LOG.warning("Leader heartbeat timeout; triggering election")
                detector.seen()
                self.election.start()

    def _yield_to_a_higher_broker(self) -> None:
        """Bully rule: the highest live id leads. A leader that finds a higher-id broker answering (it came
        back, or the cluster briefly split in two) runs an election, which the higher broker wins and
        announces, so there is never a lasting second leader."""
        for peer_id in sorted(self.peers):
            if peer_id <= self.broker_id:
                continue
            answer = self._peer_request(peer_id, {"type": "heartbeat", "sender_id": self.broker_id, "role": "broker"})
            if answer:
                LOG.warning("Broker %s is alive with a higher id (leader=%s); yielding leadership", peer_id, answer.get("is_leader"))
                self.election.start()
                return

    def _worker_reaper(self) -> None:
        while not self.stop_event.wait(config.HEARTBEAT_INTERVAL):
            now = time.monotonic()
            for worker in self.registry.all():
                if now - worker.last_seen > config.WORKER_STALE_TIMEOUT:
                    removed = self.registry.remove(worker.worker_id)
                    if removed:
                        LOG.warning("Worker %s expired after missed heartbeats", worker.worker_id)
                        self._on_worker_gone(removed)
            if self.is_leader:
                self._redeliver_stuck()
                self._expire_waiting()

    def _track_client(self, conn: socket.socket) -> None:
        with self._clients_lock:
            self._clients.add(conn)

    def _untrack_client(self, conn: socket.socket) -> None:
        with self._clients_lock:
            self._clients.discard(conn)

    def _disconnect_clients(self) -> None:
        self.registry.clear()
        with self._clients_lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass

    def shutdown(self) -> None:
        if self.stop_event.is_set() and self.server is None:
            return
        self.stop_event.set()
        self._disconnect_clients()
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        LOG.info("Broker %s stopped", self.broker_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="broker ID from common/config.py")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    broker = Broker(args.id)
    signal.signal(signal.SIGINT, lambda *_: broker.shutdown())
    signal.signal(signal.SIGTERM, lambda *_: broker.shutdown())
    broker.serve_forever()


if __name__ == "__main__":
    main()
