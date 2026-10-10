"""Replicated Polyglot Relay broker with Bully election and worker pools."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from common import config
from common.election import BullyElection
from common.heartbeat import FailureDetector
from common.protocol import ProtocolError, decode_stream, encode, recv_one


LOG = logging.getLogger("polyglot.broker")


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

    def send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            self.sock.sendall(encode(message))


class WorkerRegistry:
    """Thread-safe dynamic registry with independent round-robin cursors."""

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

    def choose(self, lang: str) -> WorkerConnection | None:
        with self._lock:
            pool = self._workers.get(lang, [])
            if not pool:
                return None
            index = self._cursor[lang] % len(pool)
            self._cursor[lang] = (index + 1) % len(pool)
            return pool[index]

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
        was_serving = bool(self.registry.all())
        LOG.info("Leader is broker %s%s", leader_id, " (this replica)" if self.is_leader else "")
        if not self.is_leader and was_serving:
            LOG.info("Demoted; disconnecting clients so they can discover the new leader")
            self._disconnect_clients()

    def _peer_request(self, peer_id: int, message: dict) -> dict | None:
        peer = self.peers.get(peer_id)
        if not peer:
            return None
        try:
            with socket.create_connection((peer["host"], peer["port"]), config.CONNECT_TIMEOUT) as sock:
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
                for message in messages:
                    worker.last_seen = time.monotonic()
                    if message["type"] == "translated_chunk":
                        self.update_job(message, "completed")
                        self._publish_translation(message)
                    elif message["type"] == "chunk_status":
                        self.update_job(message, str(message.get("status", "processing")))
            elif role == "stt":
                conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": self.broker_id}))
                self._track_client(conn)
                LOG.info("STT producer registered from %s:%s", *address)
                for message in messages:
                    if message["type"] == "transcript_chunk":
                        self.route_chunk(message)
            else:
                conn.sendall(encode({"type": "error", "message": "unknown registration role"}))
        except (StopIteration, ConnectionError, OSError, ProtocolError, ValueError) as exc:
            LOG.debug("Connection %s ended: %s", address, exc)
        finally:
            if worker and self.registry.remove(worker.worker_id, close=False):
                LOG.info("Worker deregistered: %s (%s)", worker.worker_id, worker.lang)
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

    def route_chunk(self, chunk: dict) -> None:
        for lang in config.TARGET_LANGUAGES:
            if lang == chunk.get("source_lang"):
                result = {**chunk, "type": "translated_chunk", "lang": lang,
                          "worker_id": f"broker-{self.broker_id}", "translation_backend": "passthrough"}
                self.update_job(result, "source")
                self._publish_translation(result)
                continue
            attempts = len(self.registry.snapshot(lang))
            delivered = False
            for _ in range(attempts):
                worker = self.registry.choose(lang)
                if not worker:
                    break
                try:
                    self.update_job({**chunk, "lang": lang, "worker_id": worker.worker_id}, "queued")
                    worker.send(chunk)
                    LOG.info("Forwarded chunk %s to %s worker %s", chunk.get("chunk_id"), lang, worker.worker_id)
                    delivered = True
                    break
                except OSError:
                    self.registry.remove(worker.worker_id)
                    LOG.warning("Removed unreachable worker %s", worker.worker_id)
            if not delivered:
                self.update_job({**chunk, "lang": lang}, "unavailable")
                LOG.warning("No available %s worker for chunk %s", lang, chunk.get("chunk_id"))

    def update_job(self, message: dict, status: str) -> None:
        key = (message.get("chunk_id"), message.get("lang"))
        with self._jobs_lock:
            row = self.jobs.setdefault(key, {"chunk_id": key[0], "lang": key[1]})
            row.update(status=status, updated_at=time.time())
            for field in ("worker_id", "detail"):
                if field in message:
                    row[field] = message[field]
            while len(self.jobs) > 1800:
                self.jobs.popitem(last=False)

    def telemetry(self) -> dict:
        registered = {w.worker_id for w in self.registry.all()}
        with self._jobs_lock:
            for row in self.jobs.values():
                if row["status"] in ("queued", "processing", "quota_wait", "retrying", "translating") and row.get("worker_id") not in registered:
                    row.update(status="interrupted", detail="Worker disconnected; resubmit this text")
            jobs = [dict(row) for row in self.jobs.values()]
        members = [{"worker_id": w.worker_id, "lang": w.lang,
                    "mode": w.mode,
                    "heartbeat_age": round(time.monotonic() - w.last_seen, 1)}
                   for w in self.registry.all()]
        return {"members": members, "jobs": jobs}

    def _publish_translation(self, message: dict) -> None:
        lang = str(message.get("lang"))
        for worker in self.registry.snapshot(lang):
            try:
                worker.send(message)
            except OSError:
                self.registry.remove(worker.worker_id)

    def _leader_monitor(self) -> None:
        # Wall-clock detector: a probe to a dead leader can itself block for
        # CONNECT_TIMEOUT, so counting failed polls (one INTERVAL each) made
        # detection take several times HEARTBEAT_TIMEOUT.
        detector = FailureDetector(config.HEARTBEAT_TIMEOUT)
        while not self.stop_event.wait(config.HEARTBEAT_INTERVAL):
            if self.is_leader:
                detector.seen()
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

    def _worker_reaper(self) -> None:
        while not self.stop_event.wait(config.HEARTBEAT_INTERVAL):
            now = time.monotonic()
            for worker in self.registry.all():
                if now - worker.last_seen > config.WORKER_STALE_TIMEOUT:
                    if self.registry.remove(worker.worker_id):
                        LOG.warning("Worker %s expired after missed heartbeats", worker.worker_id)

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
