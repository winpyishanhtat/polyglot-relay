"""Interactive web control room for the Polyglot Relay cluster."""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import queue
import socket
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from common import config
from common.discovery import discover_worker
from common.protocol import ProtocolError, decode_stream, encode, recv_one
from stt_service import DEMO_SENTENCES, TranscriptProducer, transcribe_file

LOG = logging.getLogger("polyglot.ui")
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "ui"
# Whisper model used to transcribe browser uploads/recordings - same default
# as stt_service.py's --model flag, overridable the same way (env var, since
# the dashboard has no CLI flags of its own for this).
WHISPER_MODEL = os.getenv("POLYGLOT_WHISPER_MODEL", "base")
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_LIVE_SLICE_BYTES = 2 * 1024 * 1024
# Slices waiting for the transcriber. Deliberately small: in a live session a
# caption that arrives half a minute late is worse than a skipped slice.
LIVE_QUEUE_SLICES = 3


class LiveBacklogError(Exception):
    """The live transcriber is saturated; the caller should skip, not retry."""


class DashboardState:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.captions = {lang: deque(maxlen=50) for lang in config.TARGET_LANGUAGES}
        self.workers = {lang: {"connected": False, "worker_id": None} for lang in config.TARGET_LANGUAGES}
        self.activity: deque[dict[str, Any]] = deque(maxlen=40)
        self.pending = 0
        self.media = {"status": "idle", "chunks": 0}
        self.live = {"slices": 0, "chunks": 0, "dropped": 0, "queued": 0, "last_slice_at": None}
        self.chunks: dict[str, dict] = {}
        self.sequence = 0
        # The original (pre-translation) text for a chunk, keyed by
        # chunk_id, recorded once at submission time so every language's
        # translated caption can also show what was actually said - a
        # bounded deque of the keys themselves drives eviction, since a
        # plain dict has no cheap way to expire its oldest entries.
        self.originals: dict[str, dict[str, str]] = {}
        self._originals_order: deque[str] = deque(maxlen=200)
        self._lock = threading.RLock()

    def event(self, message: str, kind: str = "info") -> None:
        with self._lock:
            self.activity.appendleft({"message": message, "kind": kind, "timestamp": time.time()})

    def record_original(self, chunk_id: str, text: str, source_lang: str | None) -> None:
        with self._lock:
            if chunk_id not in self.originals and len(self._originals_order) == self._originals_order.maxlen:
                expired = self._originals_order.popleft()
                self.originals.pop(expired, None)
                self.chunks.pop(expired, None)
            self.originals[chunk_id] = {"text": text, "source_lang": source_lang or "auto"}
            self._originals_order.append(chunk_id)
            self.sequence += 1
            self.chunks[chunk_id] = {"chunk_id": chunk_id, "number": self.sequence,
                "text": text, "source_lang": source_lang, "created_at": time.time(),
                "targets": {lang: {"status": "source" if lang == source_lang else "awaiting_broker"}
                            for lang in config.TARGET_LANGUAGES}}

    def add_caption(self, message: dict[str, Any]) -> None:
        lang = str(message["lang"])
        chunk_id = message.get("chunk_id")
        with self._lock:
            original = self.originals.get(chunk_id) if chunk_id else None
            if any(item["chunk_id"] == chunk_id for item in self.captions[lang]):
                return
            chunk = self.chunks.get(chunk_id)
            if chunk:
                chunk["targets"][lang] = {"status": "source" if lang == chunk["source_lang"] else "completed",
                                          "worker_id": message.get("worker_id")}
            self.captions[lang].appendleft({
                "chunk_id": chunk_id,
                "text": str(message.get("text", "")),
                "timestamp": float(message.get("timestamp", time.time())),
                "worker_id": message.get("worker_id"),
                "translation_backend": message.get("translation_backend", "relay"),
                "original_text": original["text"] if original else None,
                "original_source_lang": original["source_lang"] if original else None,
                "number": chunk["number"] if chunk else None,
            })
        self.event(f"{lang.upper()} caption received", "caption")

    def worker_status(self, lang: str, connected: bool, worker_id: str | None = None) -> None:
        with self._lock:
            before = self.workers[lang]["connected"]
            self.workers[lang] = {"connected": connected, "worker_id": worker_id if connected else None}
        if before != connected:
            self.event(f"{lang.upper()} listener {'connected' if connected else 'reconnecting'}",
                       "success" if connected else "warning")

    def clear(self) -> None:
        with self._lock:
            for captions in self.captions.values():
                captions.clear()
        self.event("Caption history cleared")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "captions": {lang: list(items) for lang, items in self.captions.items()},
                "workers": {lang: dict(value) for lang, value in self.workers.items()},
                "activity": list(self.activity),
                "pending": self.pending,
                "media": dict(self.media),
                "live": dict(self.live),
                "chunks": json.loads(json.dumps(list(self.chunks.values()))),
                "uptime": int(time.time() - self.started_at),
            }


class DashboardRuntime:
    def __init__(self) -> None:
        self.state = DashboardState()
        self.stop_event = threading.Event()
        self.outbox: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=100)
        self.live_queue: queue.Queue[tuple[bytes, str]] = queue.Queue(maxsize=LIVE_QUEUE_SLICES)
        self.producer = TranscriptProducer()
        self._media_lock = threading.Lock()
        self._last_leader = None

    def start(self) -> None:
        for lang in config.TARGET_LANGUAGES:
            threading.Thread(target=self._watch_language, args=(lang,), name=f"watch-{lang}", daemon=True).start()
        threading.Thread(target=self._send_loop, name="caption-sender", daemon=True).start()
        threading.Thread(target=self._live_loop, name="live-transcriber", daemon=True).start()
        self.state.event("Dashboard connected to the relay network", "success")

    def submit(self, text: str, source_lang: str | None = None) -> None:
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            raise ValueError("Caption text cannot be empty")
        if len(cleaned) > 2000:
            raise ValueError("Caption text must be 2,000 characters or fewer")
        if source_lang and source_lang != "auto" and source_lang not in config.LANGUAGES:
            raise ValueError(f"Unknown source language {source_lang!r}")
        with self.state._lock:
            active = sum(any(t["status"] not in ("source", "completed", "failed", "unavailable", "interrupted")
                             for t in c["targets"].values()) for c in self.state.chunks.values())
            if active + self.state.pending >= 100:
                raise ValueError("100 chunks are still processing. Wait for translations before submitting more.")
            self.state.pending += 1
        self.outbox.put((cleaned, source_lang))
        self.state.event("Caption queued for translation")

    def submit_audio(self, data: bytes, source_lang: str | None = None,
                     allow_empty: bool = False) -> tuple[int, str | None]:
        """Transcribe an uploaded/recorded audio-or-video blob with Whisper,
        in-process, and queue each resulting chunk exactly like submit().
        Returns (chunk count, the language Whisper actually detected - only
        meaningful when source_lang was "auto"/unset, None otherwise) so the
        caller can show the user what was detected, not just how many
        captions came out of it.

        Transcription happens here rather than over the broker/worker wire
        protocol: audio_pcm_b64 (used by translator_worker.py's Gemini Live
        path) is raw 16kHz PCM, not an arbitrary compressed container, and
        broadcasting a multi-MB blob to every language worker just to get
        text out of it would be pure overhead. Only the transcribed text
        ever leaves this method.
        """
        if not data:
            raise ValueError("Uploaded audio/video is empty")
        if source_lang and source_lang != "auto" and source_lang not in config.LANGUAGES:
            raise ValueError("Select one of the nine supported source languages")
        # A concrete source_lang (the dashboard's "Speaking in" dropdown,
        # anything but "auto") is passed to Whisper too, not just attached
        # as caption metadata - telling it the language up front skips its
        # own detection pass entirely, which is both faster and more
        # accurate than guessing, especially for languages Whisper is
        # weaker at recognizing unprompted.
        language_hint = source_lang if source_lang and source_lang != "auto" else None
        with tempfile.NamedTemporaryFile(suffix=".media", delete=False) as tmp:
            tmp.write(data)
            tmp_path = pathlib.Path(tmp.name)
        try:
            chunks = transcribe_file(tmp_path, WHISPER_MODEL, language=language_hint)
            count = 0
            detected_summary: str | None = None
            for text, _pcm, detected_lang in chunks:
                # Whisper's own per-file detection (only meaningful when
                # the dropdown was left on "auto") beats staying tagged
                # "auto" for the rest of the pipeline - it unlocks the
                # matching worker's passthrough and every other worker's
                # known-source Gemini prompt, instead of every worker
                # guessing independently. An explicit dropdown pick always
                # wins regardless, same as the language hint above.
                effective_source_lang = source_lang
                if (not effective_source_lang or effective_source_lang == "auto") and detected_lang in config.LANGUAGES:
                    effective_source_lang = detected_lang
                    if detected_summary is None:
                        detected_summary = detected_lang
                self.submit(text, effective_source_lang)
                count += 1
                with self.state._lock:
                    self.state.media.update(chunks=count)
                self.state.event(f"Audio chunk {count} transcribed and queued", "success")
            if not count:
                if allow_empty:  # a silent live slice is normal, not an error
                    return 0, None
                raise ValueError("No speech detected in the uploaded audio")
            return count, detected_summary
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            raise ValueError(f"Could not transcribe upload: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

    def start_audio(self, data: bytes, source_lang: str | None) -> None:
        if not data or source_lang not in config.LANGUAGES:
            raise ValueError("Select a source language and provide an audio/video file")
        if not self._media_lock.acquire(blocking=False):
            raise ValueError("An upload is already being transcribed; wait for it to finish")
        with self.state._lock:
            self.state.media = {"status": "transcribing", "chunks": 0, "source_lang": source_lang}
        def process():
            try:
                count, _ = self.submit_audio(data, source_lang)
                with self.state._lock:
                    self.state.media.update(status="transcribed", chunks=count)
                self.state.event("Transcription finished; language pools continue processing", "success")
            except Exception as exc:
                with self.state._lock:
                    self.state.media.update(status="failed", error=str(exc))
                self.state.event(str(exc), "error")
            finally:
                self._media_lock.release()
        threading.Thread(target=process, name="upload-transcriber", daemon=True).start()

    def enqueue_live_slice(self, data: bytes, source_lang: str | None) -> int:
        """Accept one self-contained audio slice from a live browser session.

        Returns immediately (the browser must not wait on Whisper). If the
        transcriber is already behind, the slice is rejected rather than
        queued without bound - see LIVE_QUEUE_SLICES.
        """
        if not data:
            raise ValueError("Live audio slice is empty")
        if source_lang not in config.LANGUAGES:
            raise ValueError("Select the spoken language before going live")
        try:
            self.live_queue.put_nowait((data, source_lang))
        except queue.Full:
            with self.state._lock:
                self.state.live["dropped"] += 1
            self.state.event("Live transcription is behind; a slice was skipped", "warning")
            raise LiveBacklogError("Transcriber is behind; slice skipped") from None
        with self.state._lock:
            self.state.live["queued"] = self.live_queue.qsize()
        return self.live_queue.qsize()

    def _live_loop(self) -> None:
        # One consumer: slices are transcribed strictly in arrival order, and
        # the shared media lock keeps Whisper from running two jobs at once.
        while not self.stop_event.is_set():
            try:
                data, source_lang = self.live_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._media_lock:
                    count, _ = self.submit_audio(data, source_lang, allow_empty=True)
                with self.state._lock:
                    self.state.live["slices"] += 1
                    self.state.live["chunks"] += count
                    self.state.live["last_slice_at"] = time.time()
            except Exception as exc:
                LOG.warning("Live slice failed: %s", exc)
                self.state.event(f"Live slice failed: {exc}", "error")
            finally:
                with self.state._lock:
                    self.state.live["queued"] = self.live_queue.qsize()
                self.live_queue.task_done()

    def run_demo(self) -> None:
        for sentence in DEMO_SENTENCES:
            self.submit(sentence, source_lang="en")
        self.state.event("Four-caption demo queued", "success")

    def _send_loop(self) -> None:
        sequence = 0
        while not self.stop_event.is_set():
            try:
                text, source_lang = self.outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            sequence += 1
            try:
                chunk_id = f"ui-{int(time.time() * 1000)}-{sequence}"
                self.state.record_original(chunk_id, text, source_lang)
                if source_lang in config.LANGUAGES:
                    self.state.add_caption({"lang": source_lang, "chunk_id": chunk_id,
                        "text": text, "translation_backend": "passthrough", "worker_id": "source"})
                self.producer.send(text, chunk_id, source_lang=source_lang)
                self.state.event("Caption sent to the leader broker", "success")
            except Exception as exc:
                LOG.exception("Caption submission failed")
                self.state.event(f"Caption failed: {exc}", "error")
            finally:
                with self.state._lock:
                    self.state.pending = max(0, self.state.pending - 1)
                self.outbox.task_done()

    def _watch_language(self, lang: str) -> None:
        while not self.stop_event.is_set():
            endpoint = discover_worker(lang)
            if not endpoint:
                self.state.worker_status(lang, False)
                self.stop_event.wait(config.RECONNECT_DELAY)
                continue
            try:
                with socket.create_connection((endpoint["host"], endpoint["port"]), config.CONNECT_TIMEOUT) as sock:
                    sock.settimeout(None)
                    self.state.worker_status(lang, True, str(endpoint["worker_id"]))
                    for message in decode_stream(sock):
                        if message["type"] == "translated_chunk":
                            self.state.add_caption(message)
            except (OSError, ConnectionError, ProtocolError) as exc:
                LOG.debug("%s listener reconnecting: %s", lang, exc)
            self.state.worker_status(lang, False)
            self.stop_event.wait(config.RECONNECT_DELAY)

    def broker_status(self) -> list[dict[str, Any]]:
        statuses = []
        for broker in config.BROKERS:
            status = {"id": broker["id"], "online": False, "leader": False}
            try:
                with socket.create_connection((broker["host"], broker["port"]), config.CONNECT_TIMEOUT) as sock:
                    sock.settimeout(config.CONNECT_TIMEOUT)
                    sock.sendall(encode({"type": "heartbeat", "sender_id": "dashboard", "role": "broker"}))
                    response = recv_one(sock)
                    status.update(online=True, leader=bool(response.get("is_leader")),
                                  leader_id=response.get("leader_id"), members=response.get("members", []),
                                  jobs=response.get("jobs", []))
            except (OSError, ConnectionError, ProtocolError):
                pass
            statuses.append(status)
        return statuses

    def status(self) -> dict:
        brokers = self.broker_status()
        leader = next((b for b in brokers if b["online"] and b["leader"]), None)
        with self.state._lock:
            if leader and self._last_leader is not None and leader["id"] != self._last_leader:
                for chunk in self.state.chunks.values():
                    for target in chunk["targets"].values():
                        if target["status"] not in ("completed", "source", "failed", "unavailable"):
                            target.update(status="interrupted", detail="Leader changed; resubmit this text if no result arrives")
                self.state.event(f"Leader changed to broker {leader['id']}; check interrupted chunks", "warning")
            if leader:
                self._last_leader = leader["id"]
            for job in (leader or {}).get("jobs", []):
                chunk = self.state.chunks.get(job["chunk_id"])
                if chunk and job["lang"] in chunk["targets"]:
                    current = chunk["targets"][job["lang"]]
                    if current["status"] not in ("completed", "source"):
                        chunk["targets"][job["lang"]] = dict(job)
            snapshot = self.state.snapshot()
        snapshot["brokers"] = brokers
        snapshot["settings"] = {"tier": os.getenv("GEMINI_TIER", "free"),
            "chunk_seconds": float(os.getenv("POLYGLOT_FILE_CHUNK_SECONDS", "15")),
            "live_slice_seconds": max(2.0, min(10.0, float(os.getenv("POLYGLOT_LIVE_SLICE_SECONDS", "4")))),
            "rpm": int(os.getenv("GEMINI_RATE_LIMIT_PER_MINUTE", "5"))}
        return snapshot

    def stop(self) -> None:
        self.stop_event.set()
        self.producer.close()


class DashboardHandler(BaseHTTPRequestHandler):
    runtime: DashboardRuntime

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10_000:
            raise ValueError("Request is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _raw_body(self, max_bytes: int) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is empty")
        if length > max_bytes:
            raise ValueError(f"Upload too large (max {max_bytes // (1024 * 1024)}MB)")
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/status":
            return self._json(self.runtime.status())
        if self.path == "/api/captions":
            return self._json(self.runtime.state.snapshot()["captions"])
        if self.path == "/api/languages":
            languages = [{"code": code, **meta} for code, meta in config.LANGUAGES.items()]
            return self._json({"languages": languages})
        if self.path == "/health":
            return self._json({"status": "ok"})
        route = "/index.html" if self.path == "/" else self.path.split("?", 1)[0]
        files = {"/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/app.css": ("app.css", "text/css; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        if route not in files:
            return self.send_error(HTTPStatus.NOT_FOUND)
        filename, content_type = files[route]
        payload = (STATIC_DIR / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/captions":
                body = self._body()
                self.runtime.submit(str(body.get("text", "")), body.get("source_lang") or None)
                return self._json({"accepted": True}, HTTPStatus.ACCEPTED)
            if self.path.startswith("/api/captions/audio"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                source_lang = (query.get("source_lang", [None])[0]) or None
                data = self._raw_body(MAX_AUDIO_UPLOAD_BYTES)
                self.runtime.start_audio(data, source_lang)
                return self._json({"accepted": True, "status": "transcribing"}, HTTPStatus.ACCEPTED)
            if self.path.startswith("/api/live/slice"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                source_lang = (query.get("source_lang", [None])[0]) or None
                data = self._raw_body(MAX_LIVE_SLICE_BYTES)
                queued = self.runtime.enqueue_live_slice(data, source_lang)
                return self._json({"accepted": True, "queued": queued}, HTTPStatus.ACCEPTED)
            if self.path == "/api/demo":
                self.runtime.run_demo()
                return self._json({"accepted": True, "count": len(DEMO_SENTENCES)}, HTTPStatus.ACCEPTED)
            if self.path == "/api/clear":
                self.runtime.state.clear()
                return self._json({"cleared": True})
            self.send_error(HTTPStatus.NOT_FOUND)
        except LiveBacklogError as exc:
            self._json({"error": str(exc), "dropped": True}, HTTPStatus.TOO_MANY_REQUESTS)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug("HTTP %s", format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = DashboardRuntime()
    DashboardHandler.runtime = runtime
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    runtime.start()
    LOG.info("Dashboard ready at http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        runtime.stop()


if __name__ == "__main__":
    main()
