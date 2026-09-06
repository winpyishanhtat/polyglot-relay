"""Dynamically registered translation worker and listener caption server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from common import config
from common.discovery import connect_to_leader
from common.protocol import ProtocolError, decode_stream, encode

LOG = logging.getLogger("polyglot.worker")

_RETRYABLE_STATUS = {429, 500, 503}

# Confirmed permanently absent from Argos Translate's catalog (the engine
# behind LibreTranslate) - not a per-deployment config choice that might
# change, so there's no point ever asking LibreTranslate about these three
# (even a cached "no" costs a request once a minute); skip straight to
# Gemini instead.
_LIBRETRANSLATE_NEVER_SUPPORTS = frozenset({"my", "km", "lo"})


def _is_retryable(exc: Exception) -> bool:
    """True for rate-limit/transient errors the Gemini SDK raises as
    google.genai.errors.ClientError/ServerError with a numeric .code, or as
    a plain exception whose message mentions the same conditions - or a
    bare connection-level failure from the underlying httpx client (e.g.
    "Server disconnected without sending a response"), which is a generic
    transient network blip rather than anything Gemini-specific and is
    just as worth a retry as an explicit 503."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True
    text = str(exc)
    return any(marker in text for marker in (
        "RESOURCE_EXHAUSTED", "UNAVAILABLE", " 429", " 503",
        "Server disconnected", "Connection reset", "ConnectionError", "connection closed",
    ))


_RETRY_DELAY_PATTERN = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


def _suggested_retry_delay(exc: Exception) -> float | None:
    """A 429's payload usually includes Google's own retryDelay (e.g.
    "retryDelay": "20s") - the actual time left until the per-minute window
    clears. Our exponential backoff (starting at ~1-1.5s) is far shorter
    than that, so retrying on our own schedule mostly just re-fails against
    the same still-active window - wasting an attempt, and worse, quota
    that counts against the much scarcer daily cap, before the limit has
    genuinely lifted. Respecting Google's own number avoids those
    guaranteed-to-fail early retries."""
    match = _RETRY_DELAY_PATTERN.search(str(exc))
    return float(match.group(1)) if match else None


def _is_daily_quota_exhausted(exc: Exception) -> bool:
    """A 429 whose quotaId mentions PerDay is a daily cap, not a per-minute
    one - Google's own suggested retryDelay in this case is misleading
    (often a handful of seconds) because the quota genuinely will not free
    up again for hours. Retrying it burns time and log noise for nothing,
    so this is treated as non-retryable even though it is a 429."""
    text = str(exc)
    return "PerDay" in text or "RequestsPerDay" in text


# Khmer and Lao specifically have far less training data available to
# Gemini than the other 7 languages here (Burmese, despite also being a
# LibreTranslate gap, translates noticeably better in practice) - no
# amount of prompting fully closes a base-model data gap like that, but
# asking for deliberate care rather than a fast guess is a real, low-cost
# nudge in the right direction.
_LOW_RESOURCE_LANGUAGES = frozenset({"km", "lo"})


def translation_instruction(target_lang: str, source_lang: str | None = None) -> str:
    """Builds the Gemini system_instruction for one text-translation call.
    Pulled out of Translator._translate_text as a standalone function (no
    google-genai import needed) so prompt wording can be tested directly,
    including in the lightweight `mock`-based test image that doesn't
    install that SDK."""
    care_note = (
        f" {config.language_label(target_lang)} has less training data available to you than most "
        "languages you work with - go slowly, and prefer a grammatically careful, natural rendering "
        "over a fast literal guess."
    ) if target_lang in _LOW_RESOURCE_LANGUAGES else ""
    # Context matters here beyond "translate X to Y": these are live
    # spoken-conversation fragments (often incomplete sentences, since
    # captions arrive in short chunks), and a stiff literal translation
    # reads as unnatural/wrong to a native speaker compared to how they'd
    # actually phrase the same idea - a real difference for languages with
    # strong register/idiom conventions (Thai, Burmese, Vietnamese, etc.).
    if source_lang:
        return (
            "You are captioning a live, real-time spoken conversation for a multilingual "
            f"audience. Translate this transcript fragment, spoken in {config.language_label(source_lang)}, "
            f"into natural, conversational {config.language_label(target_lang)} - the way a native "
            "speaker would actually say it, not a stiff word-for-word translation. It may be an "
            "incomplete sentence (live captions arrive in short chunks); translate it as-is rather "
            "than trying to complete or restructure it. Preserve the speaker's tone (casual, formal, "
            "urgent, etc.), proper names, and numbers exactly." + care_note + " Return only the "
            "translated caption - no explanation, labels, quotes, or Markdown."
        )
    return (
        "You are captioning a live, real-time spoken conversation for a multilingual "
        "audience. Identify the spoken language, then translate this transcript fragment "
        f"into natural, conversational {config.language_label(target_lang)} - the way a native "
        "speaker would actually say it, not a stiff word-for-word translation. It may be an "
        "incomplete sentence (live captions arrive in short chunks); translate it as-is rather "
        "than trying to complete or restructure it. Preserve the speaker's tone (casual, formal, "
        f"urgent, etc.), proper names, and numbers exactly." + care_note + " If it is already in "
        f"{config.language_label(target_lang)}, return it unchanged. Return only the translated "
        "caption - no explanation, labels, quotes, or Markdown."
    )


def _libretranslate_post(url: str, payload: dict, timeout: float = 8.0) -> dict:
    """Bare urllib POST - deliberately not using requests/httpx so the
    lightweight `translate` image doesn't need an extra dependency just for
    this optional backend."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _libretranslate_get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Translator:
    def __init__(self, lang: str, mock: bool = False) -> None:
        self.lang, self.mock, self._client = lang, mock, None
        self.report_status = lambda status, detail="": None
        # GEMINI_TIER picks the defaults below; it never overrides an
        # explicitly-set GEMINI_TEXT_MODEL / POLYGLOT_STAGGER_STEP_SECONDS.
        # "paid" (default): a billing-enabled Google AI Studio / Cloud key.
        # Requests-per-minute quota is high enough that a 15-way broadcast
        # no longer needs to be spread out, and the higher-quality Pro model
        # is affordable to use by default. "free": the original free-tier
        # defaults - staggered requests and the cheaper Flash model, since
        # free-tier RPM limits are low enough that firing all 15 at once
        # trips them (see the stagger comment below).
        self.tier = (os.getenv("GEMINI_TIER") or "paid").strip().lower()
        # "gemini-pro-latest" is Google's own always-current alias for the
        # Pro model family (it resolves server-side to whichever dated model,
        # e.g. gemini-3.1-pro, is current) - there is no "gemini-3.6-pro";
        # that name doesn't exist and 404s. Pro-tier access itself requires
        # a billing-enabled key: on the free tier its quota is a hard 0, not
        # just a low limit (confirmed via a live 429 RESOURCE_EXHAUSTED with
        # quotaId "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" and
        # limit: 0 for gemini-3.1-pro) - so GEMINI_TIER=paid only actually
        # works once the underlying Google Cloud project has billing enabled.
        default_text_model = "gemini-3.6-flash" if self.tier == "free" else "gemini-pro-latest"
        default_stagger_step = "0.08" if self.tier == "free" else "0.0"
        # `or` rather than getenv's own default: Docker Compose commonly
        # passes these through as an empty string (${VAR:-}) rather than
        # leaving them unset when the host doesn't override them, and an
        # empty string must fall back to the tier default just like "unset"
        # does, not be used verbatim as a model name.
        self.text_model = os.getenv("GEMINI_TEXT_MODEL") or default_text_model
        self.live_model = os.getenv("GEMINI_LIVE_MODEL") or "gemini-3.5-live-translate-preview"
        self.live_timeout = float(os.getenv("GEMINI_LIVE_TIMEOUT", "30"))
        self.max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "5"))
        # "auto" (default): try the free LibreTranslate service first, fall
        # back to Gemini only for a language LibreTranslate doesn't have
        # loaded. "libretranslate": free only, never falls back (and raises
        # clearly if unsupported). "gemini": skip LibreTranslate entirely -
        # the original behavior, for when you don't want to run it at all.
        self.backend_preference = os.getenv("TRANSLATION_BACKEND", "auto")
        self.libretranslate_url = os.getenv("LIBRETRANSLATE_URL", "http://libretranslate:5000").rstrip("/")
        self._lt_supported: bool | None = None
        self._lt_checked_at = 0.0
        # A single broadcast fans out to every language pool at once (see
        # broker.route_chunk); on GEMINI_TIER=free, without spreading them
        # out, all 15 workers hit the Gemini API in the same instant, which
        # is what was tripping the free-tier rate limit after only 3-4
        # languages succeeded. Each worker sleeps a small, deterministic,
        # language-specific offset (0 for the 1st registered language,
        # ~0.08s more per language after that on the free tier; 0 on the
        # paid tier, whose RPM ceiling comfortably absorbs 15 concurrent
        # calls) once per chunk, before its first attempt only - retries
        # have their own backoff below.
        try:
            index = config.TARGET_LANGUAGES.index(lang)
        except ValueError:
            index = 0
        self.stagger_seconds = index * float(os.getenv("POLYGLOT_STAGGER_STEP_SECONDS", default_stagger_step))
        # Belt-and-suspenders on top of the stagger above: see
        # gemini_rate_gate.py for why a fixed per-worker offset alone isn't
        # enough to keep a real burst under the free tier's 5 req/min cap.
        # Empty/unset (e.g. on paid tier, or if the gate isn't deployed)
        # disables this and falls back to stagger_seconds + retry-on-429
        # alone.
        self.rate_gate_url = (os.getenv("GEMINI_RATE_GATE_URL") or "").rstrip("/")

    def _await_gemini_slot(self) -> None:
        if self.tier != "free" or not self.rate_gate_url:
            return
        try:
            self.report_status("quota_wait", "Waiting for the shared Gemini request budget")
            with urllib.request.urlopen(f"{self.rate_gate_url}/acquire", timeout=900):
                pass
        except Exception as exc:
            raise RuntimeError("Shared rate gate unavailable; Gemini request was not sent") from exc

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai  # type: ignore
            except ImportError as exc:
                raise RuntimeError("install google-genai or run with --mock") from exc
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError("set GEMINI_API_KEY or run with --mock")
            self._client = genai.Client(api_key=api_key, http_options={"timeout": 60000})
        return self._client

    def _call_with_retry(self, fn, *, base_delay: float = 1.0):
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                if _is_daily_quota_exhausted(exc):
                    LOG.error("Gemini daily quota exhausted for this project/model; "
                              "not retrying, it will not recover within minutes (%s)", exc)
                    raise
                if attempt == self.max_retries or not _is_retryable(exc):
                    raise
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
                suggested = _suggested_retry_delay(exc)
                if suggested is not None:
                    # Capped at 60s (the free tier's per-minute window size)
                    # so one bad Retry-After hint can't stall a chunk far
                    # longer than the situation actually warrants.
                    delay = max(delay, min(suggested, 60.0))
                LOG.warning("Gemini call failed (%s); retry %s/%s in %.1fs",
                            exc, attempt, self.max_retries, delay)
                self.report_status("retrying", f"Retry {attempt}/{self.max_retries} in {delay:.0f}s")
                time.sleep(delay)

    async def _call_with_retry_async(self, fn, *, base_delay: float = 1.5):
        for attempt in range(1, self.max_retries + 1):
            try:
                return await fn()
            except Exception as exc:
                if _is_daily_quota_exhausted(exc):
                    LOG.error("Gemini daily quota exhausted for this project/model; "
                              "not retrying, it will not recover within minutes (%s)", exc)
                    raise
                if attempt == self.max_retries or not _is_retryable(exc):
                    raise
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
                suggested = _suggested_retry_delay(exc)
                if suggested is not None:
                    delay = max(delay, min(suggested, 60.0))
                LOG.warning("Gemini Live call failed (%s); retry %s/%s in %.1fs",
                            exc, attempt, self.max_retries, delay)
                await asyncio.sleep(delay)

    def translate(self, text: str, audio_pcm_b64: str | None = None,
                  audio_rate: int = 16000, source_lang: str | None = None) -> tuple[str, str]:
        # source_lang is the caption's spoken/typed language, if known:
        # None/"auto" means "let the translator detect it" (matches every
        # language this project shipped with before this option existed). A
        # concrete code means the caller already knows it, which lets a
        # worker whose own language matches the source skip translation
        # entirely instead of round-tripping through an API to translate a
        # language into itself - one fewer concurrent call in every burst.
        known_source = source_lang if source_lang and source_lang != "auto" else None
        if self.mock:
            effective_source = known_source or "en"
            return (text if effective_source == self.lang else f"[{self.lang}] {text}"), "mock"
        if known_source and known_source == self.lang:
            return text, "passthrough"
        if audio_pcm_b64:
            if audio_rate != 16000:
                raise ValueError(f"Gemini Live requires 16 kHz PCM; received {audio_rate} Hz")
            audio = base64.b64decode(audio_pcm_b64, validate=True)
            return asyncio.run(self._translate_live(audio, known_source)), "gemini-live"
        if (self.backend_preference != "gemini"
                and self.lang not in _LIBRETRANSLATE_NEVER_SUPPORTS
                and self._libretranslate_supports_target()):
            try:
                return self._translate_via_libretranslate(text, known_source), "libretranslate"
            except Exception as exc:
                if self.backend_preference == "libretranslate":
                    raise
                LOG.warning("LibreTranslate call failed (%s); falling back to Gemini", exc)
        if self.backend_preference == "libretranslate":
            raise RuntimeError(
                f"LibreTranslate has no model loaded for {self.lang!r} and "
                "TRANSLATION_BACKEND=libretranslate forbids falling back to Gemini"
            )
        return self._translate_text(text, known_source), "gemini-text"

    def _libretranslate_supports_target(self) -> bool:
        # Cache the answer for a minute rather than forever: LibreTranslate
        # can take several minutes to finish downloading language models on
        # first boot, so a connection failure early on must not be treated
        # as a permanent "unsupported" - the next chunk a minute later
        # should notice it's ready. A definitive "language not in the list"
        # answer is just as cheap to re-check, so it isn't special-cased.
        now = time.time()
        if self._lt_supported is not None and now - self._lt_checked_at < 60:
            return self._lt_supported
        try:
            languages = _libretranslate_get(f"{self.libretranslate_url}/languages")
            codes = {entry.get("code") for entry in languages}
            self._lt_supported = self.lang in codes
        except Exception as exc:
            LOG.warning("Could not reach LibreTranslate at %s (%s)", self.libretranslate_url, exc)
            self._lt_supported = False
        self._lt_checked_at = now
        return self._lt_supported

    def _translate_via_libretranslate(self, text: str, source_lang: str | None = None) -> str:
        payload = {"q": text, "source": source_lang or "auto", "target": self.lang, "format": "text"}
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = _libretranslate_post(f"{self.libretranslate_url}/translate", payload)
                translated = (result.get("translatedText") or "").strip()
                if not translated:
                    raise RuntimeError(f"LibreTranslate returned an empty translation: {result!r}")
                return translated
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(0.8 * attempt)
        raise last_exc  # type: ignore[misc]

    def _translate_text(self, text: str, source_lang: str | None = None) -> str:
        from google.genai import types  # type: ignore

        time.sleep(self.stagger_seconds)
        instruction = translation_instruction(self.lang, source_lang)
        def request():
            self._await_gemini_slot()
            self.report_status("translating", "Gemini request in progress")
            return self._get_client().models.generate_content(
                model=self.text_model, contents=text,
                config=types.GenerateContentConfig(system_instruction=instruction))
        response = self._call_with_retry(request)
        translated = (response.text or "").strip()
        if not translated:
            raise RuntimeError("Gemini returned an empty text translation")
        return translated

    async def _translate_live(self, audio: bytes, source_lang: str | None = None) -> str:
        await asyncio.sleep(self.stagger_seconds)
        await asyncio.to_thread(self._await_gemini_slot)
        return await self._call_with_retry_async(lambda: self._translate_live_once(audio, source_lang))

    async def _translate_live_once(self, audio: bytes, source_lang: str | None = None) -> str:
        from google.genai import types  # type: ignore

        live_config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=self.lang,
                echo_target_language=True,
            ),
        )
        async with self._get_client().aio.live.connect(model=self.live_model, config=live_config) as session:
            bytes_per_100ms = 16000 * 2 // 10
            for offset in range(0, len(audio), bytes_per_100ms):
                await session.send_realtime_input(
                    audio=types.Blob(data=audio[offset:offset + bytes_per_100ms],
                                     mime_type="audio/pcm;rate=16000")
                )
            await session.send_realtime_input(audio_stream_end=True)

            async def receive_translation() -> str:
                pieces: list[str] = []
                async for response in session.receive():
                    content = response.server_content
                    if not content:
                        continue
                    if content.output_transcription and content.output_transcription.text:
                        pieces.append(content.output_transcription.text)
                    if content.turn_complete:
                        break
                return "".join(pieces).strip()

            translated = await asyncio.wait_for(receive_translation(), timeout=self.live_timeout)
            if not translated:
                raise RuntimeError("Gemini Live returned no output transcription")
            return translated


class TranslatorWorker:
    def __init__(self, lang: str, host: str, port: int, worker_id: str, mock: bool = False,
                 bind_host: str | None = None) -> None:
        self.lang, self.host, self.port, self.worker_id = lang, host, port, worker_id
        self.translator = Translator(lang, mock)
        self.bind_host = bind_host or host
        self.stop_event = threading.Event()
        self.listener_server: socket.socket | None = None
        self.listeners: set[socket.socket] = set()
        self._listeners_lock = threading.Lock()
        self._broker_send_lock = threading.Lock()
        self._work = queue.Queue(maxsize=100)

    def serve(self) -> None:
        self._start_listener_server()
        threading.Thread(target=self._translation_loop, daemon=True).start()
        while not self.stop_event.is_set():
            broker: socket.socket | None = None
            heartbeat_stop = threading.Event()
            try:
                registration = {"type": "register", "role": "worker", "lang": self.lang,
                                "worker_id": self.worker_id, "host": self.host, "port": self.port,
                                "mode": "mock" if self.translator.mock else self.translator.backend_preference}
                broker, response = connect_to_leader(registration)
                LOG.info("Registered with leader broker %s", response.get("leader_id"))
                threading.Thread(target=self._heartbeat_loop, args=(broker, heartbeat_stop), daemon=True).start()
                for message in decode_stream(broker):
                    if message["type"] == "transcript_chunk":
                        try:
                            self._work.put_nowait((broker, message))
                        except queue.Full:
                            self._send_broker(broker, {"type": "chunk_status", "chunk_id": message["chunk_id"],
                                "lang": self.lang, "worker_id": self.worker_id, "status": "failed",
                                "detail": "Worker queue full; submit a shorter recording"})
                    elif message["type"] == "translated_chunk":
                        self._broadcast(message)
            except (OSError, ConnectionError, ProtocolError) as exc:
                if not self.stop_event.is_set():
                    LOG.warning("Broker connection lost (%s); rediscovering", exc)
            finally:
                heartbeat_stop.set()
                if broker:
                    broker.close()
            self.stop_event.wait(config.RECONNECT_DELAY)

    def _translation_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                broker, message = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if broker.fileno() != -1:
                    self._process_chunk(broker, message)
            finally:
                self._work.task_done()

    def _process_chunk(self, broker: socket.socket, message: dict) -> None:
        def report(status, detail=""):
            self._send_broker(broker, {"type": "chunk_status", "chunk_id": message["chunk_id"],
                "lang": self.lang, "worker_id": self.worker_id, "status": status, "detail": detail})
        self.translator.report_status = report
        try:
            report("processing")
            translated, backend = self.translator.translate(
                str(message["text"]),
                audio_pcm_b64=message.get("audio_pcm_b64"),
                audio_rate=int(message.get("audio_rate", 16000)),
                source_lang=message.get("source_lang"),
            )
            result = {"type": "translated_chunk", "chunk_id": message["chunk_id"], "lang": self.lang,
                      "text": translated, "timestamp": time.time(),
                      "worker_id": self.worker_id, "translation_backend": backend}
            self._send_broker(broker, result)
            LOG.info("Translated chunk %s via %s", message["chunk_id"], backend)
        except Exception as exc:
            LOG.error("Translation failed for chunk %s: %s", message.get("chunk_id"), exc)
            try:
                detail = "Gemini daily quota exhausted" if _is_daily_quota_exhausted(exc) else "Translation failed; inspect worker logs"
                report("failed", detail)
            except OSError:
                pass

    def _heartbeat_loop(self, broker: socket.socket, stop: threading.Event) -> None:
        while not stop.wait(config.HEARTBEAT_INTERVAL):
            try:
                self._send_broker(broker, {"type": "heartbeat", "sender_id": self.worker_id, "role": "worker"})
            except OSError:
                try:
                    broker.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def _send_broker(self, broker: socket.socket, message: dict) -> None:
        with self._broker_send_lock:
            broker.sendall(encode(message))

    def _start_listener_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind_host, self.port))
        server.listen()
        server.settimeout(1)
        self.port = server.getsockname()[1]
        self.listener_server = server
        threading.Thread(target=self._accept_listeners, name="listener-server", daemon=True).start()
        LOG.info("Listener endpoint ready on %s:%s", self.host, self.port)

    def _accept_listeners(self) -> None:
        assert self.listener_server
        while not self.stop_event.is_set():
            try:
                conn, address = self.listener_server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._listeners_lock:
                self.listeners.add(conn)
            LOG.info("Listener connected from %s:%s", *address)

    def _broadcast(self, message: dict) -> None:
        payload = encode(message)
        with self._listeners_lock:
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener.sendall(payload)
            except OSError:
                with self._listeners_lock:
                    self.listeners.discard(listener)
                listener.close()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.listener_server:
            self.listener_server.close()
        with self._listeners_lock:
            listeners = list(self.listeners)
            self.listeners.clear()
        for listener in listeners:
            listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=config.TARGET_LANGUAGES, required=True)
    parser.add_argument("--host", default=config.DEFAULT_WORKER_HOST)
    parser.add_argument("--bind-host", default=config.DEFAULT_WORKER_BIND_HOST,
                        help="local interface for listener server; host is the advertised address")
    parser.add_argument("--port", type=int, default=0, help="listener port; 0 chooses a free port")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--mock", action="store_true",
                        help="use deterministic local translation instead of Gemini")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = TranslatorWorker(args.lang, args.host, args.port,
                              args.worker_id or f"{args.lang}-{uuid.uuid4().hex[:8]}", args.mock,
                              args.bind_host)
    try:
        worker.serve()
    except KeyboardInterrupt:
        pass
    finally:
        worker.shutdown()


if __name__ == "__main__":
    main()
