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


class DashboardState:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.captions = {lang: deque(maxlen=50) for lang in config.TARGET_LANGUAGES}
        self.workers = {lang: {"connected": False, "worker_id": None} for lang in config.TARGET_LANGUAGES}
        self.activity: deque[dict[str, Any]] = deque(maxlen=40)
        self.pending = 0
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
                self.originals.pop(self._originals_order.popleft(), None)
            self.originals[chunk_id] = {"text": text, "source_lang": source_lang or "auto"}
            self._originals_order.append(chunk_id)

    def add_caption(self, message: dict[str, Any]) -> None:
        lang = str(message["lang"])
        chunk_id = message.get("chunk_id")
        with self._lock:
            original = self.originals.get(chunk_id) if chunk_id else None
            self.captions[lang].appendleft({
                "chunk_id": chunk_id,
                "text": str(message.get("text", "")),
                "timestamp": float(message.get("timestamp", time.time())),
                "worker_id": message.get("worker_id"),
                "translation_backend": message.get("translation_backend", "relay"),
                "original_text": original["text"] if original else None,
                "original_source_lang": original["source_lang"] if original else None,
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
                "uptime": int(time.time() - self.started_at),
            }


class DashboardRuntime:
    def __init__(self) -> None:
        self.state = DashboardState()
        self.stop_event = threading.Event()
        self.outbox: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.producer = TranscriptProducer()

    def start(self) -> None:
        for lang in config.TARGET_LANGUAGES:
            threading.Thread(target=self._watch_language, args=(lang,), name=f"watch-{lang}", daemon=True).start()
        threading.Thread(target=self._send_loop, name="caption-sender", daemon=True).start()
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
            self.state.pending += 1
        self.outbox.put((cleaned, source_lang))
        self.state.event("Caption queued for translation")

    def submit_audio(self, data: bytes, source_lang: str | None = None) -> tuple[int, str | None]:
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
            try:
                chunks = list(transcribe_file(tmp_path, WHISPER_MODEL, language=language_hint))
            except SystemExit as exc:
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                raise ValueError(f"Could not transcribe upload: {exc}") from exc
            if not chunks:
                raise ValueError("No speech detected in the uploaded audio")
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
            return len(chunks), detected_summary
        finally:
            tmp_path.unlink(missing_ok=True)

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
                                  leader_id=response.get("leader_id"))
            except (OSError, ConnectionError, ProtocolError):
                pass
            statuses.append(status)
        return statuses

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
            snapshot = self.runtime.state.snapshot()
            snapshot["brokers"] = self.runtime.broker_status()
            return self._json(snapshot)
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
                count, detected_language = self.runtime.submit_audio(data, source_lang)
                return self._json({"accepted": True, "captions": count, "detected_language": detected_language},
                                   HTTPStatus.ACCEPTED)
            if self.path == "/api/demo":
                self.runtime.run_demo()
                return self._json({"accepted": True, "count": len(DEMO_SENTENCES)}, HTTPStatus.ACCEPTED)
            if self.path == "/api/clear":
                self.runtime.state.clear()
                return self._json({"cleared": True})
            self.send_error(HTTPStatus.NOT_FOUND)
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
