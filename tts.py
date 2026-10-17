"""Text-to-speech for captions, using Gemini's speech models.

A caption can be read aloud in its own language: the dashboard's Listen buttons and the listener page call
POST /api/tts, which returns a WAV. Gemini returns raw 24 kHz 16-bit mono PCM (or, on some models, a ready
WAV); both are returned as a playable WAV.

A phrase heard twice costs one call: clips are kept in memory and, with POLYGLOT_TTS_CACHE_DIR, on disk
(bounded by POLYGLOT_TTS_CACHE_MB), so they survive a restart. Every call is counted against the model's daily
quota (Google resets it at midnight Pacific time). POLYGLOT_TTS_DAILY_LIMIT, when set, is the allowance per
model: GET /api/tts/quota reports what is left so the page can warn, and once a model is used up (or Google
answers that its daily quota is exhausted) it is not called again until the reset.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import logging
import os
import re
import threading
import wave
from collections import OrderedDict

from common import config

LOG = logging.getLogger("polyglot.tts")

# BCP-47 tags the speech models understand, for each language the relay handles.
LANGUAGE_CODES = {"en": "en-US", "my": "my-MM", "id": "id-ID", "ms": "ms-MY", "th": "th-TH",
                  "vi": "vi-VN", "tl": "fil-PH", "km": "km-KH", "lo": "lo-LA"}
DEFAULT_MODEL = "gemini-3.8-flash-tts"
FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"      # tried when the first gives no audio or is rate limited
DEFAULT_VOICE = "Kore"
# Gemini's prebuilt voices; a request may pick one, and GEMINI_TTS_VOICE_<LANG> sets one per language.
VOICES = ("Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
          "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
          "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
          "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat")
WARN_WHEN_LEFT = 3          # the page warns when this few relay-voice clips are left today
MAX_TEXT_CHARS = 500
SAMPLE_RATE = 24000
CACHE_ENTRIES = 100


class TtsError(RuntimeError):
    """The speech service could not be reached or returned nothing; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _api_key(lang: str) -> str | None:
    return os.getenv(f"GEMINI_API_KEY_{lang.upper()}") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def pacific_day(now: _dt.datetime | None = None) -> str:
    """Google's quota day, which turns over at midnight Pacific time."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # no time-zone data on this machine: Pacific standard time is close enough
        return (now - _dt.timedelta(hours=8)).date().isoformat()


class QuotaTracker:
    """Calls made per model today, kept in <cache dir>/quota.json when there is a cache dir."""

    def __init__(self, path: str | None = None, limit: int | None = None) -> None:
        self.path = path
        self._fixed_limit = limit
        self._lock = threading.Lock()
        self._day = pacific_day()
        self._used: dict[str, int] = {}
        self._exhausted: set[str] = set()
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                if saved.get("day") == self._day:
                    self._used = {str(k): int(v) for k, v in saved.get("used", {}).items()}
                    self._exhausted = set(saved.get("exhausted", []))
            except (OSError, ValueError, AttributeError):
                pass

    @property
    def limit(self) -> int:
        """Calls allowed per model per day; 0 means no limit is known. Read each time, so it can be changed."""
        if self._fixed_limit is not None:
            return self._fixed_limit
        return max(0, int(os.getenv("POLYGLOT_TTS_DAILY_LIMIT") or "0"))

    def _roll(self) -> None:
        today = pacific_day()
        if today != self._day:
            self._day, self._used, self._exhausted = today, {}, set()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"day": self._day, "used": self._used, "exhausted": sorted(self._exhausted)}, handle)
            os.replace(temporary, self.path)
        except OSError as exc:
            LOG.debug("Could not save the speech quota count: %s", exc)

    def allowed(self, model: str) -> bool:
        with self._lock:
            self._roll()
            if model in self._exhausted:
                return False
            return not self.limit or self._used.get(model, 0) < self.limit

    def record(self, model: str) -> None:
        with self._lock:
            self._roll()
            self._used[model] = self._used.get(model, 0) + 1
            self._save()

    def exhaust(self, model: str) -> None:
        """Google said this model's daily quota is used up: do not call it again today."""
        with self._lock:
            self._roll()
            self._exhausted.add(model)
            self._save()

    def report(self, models: list[str]) -> dict:
        with self._lock:
            self._roll()
            per_model = []
            for model in models:
                used = self._used.get(model, 0)
                if model in self._exhausted:
                    left = 0
                elif self.limit:
                    left = max(0, self.limit - used)
                else:
                    left = None
                per_model.append({"model": model, "used": used, "left": left})
            known = [m["left"] for m in per_model if m["left"] is not None]
            left = sum(known) if len(known) == len(per_model) else None
            return {"day": self._day, "limit_per_model": self.limit or None, "models": per_model, "left": left,
                    "low": left is not None and left <= WARN_WHEN_LEFT, "exhausted": left == 0}


class SpeechSynthesizer:
    """Thread-safe: at most `concurrency` calls at once, finished clips cached in memory and optionally on disk."""

    def __init__(self, concurrency: int = 2, cache_dir: str | None = None) -> None:
        self._cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(concurrency)
        self.cache_dir = cache_dir if cache_dir is not None else (os.getenv("POLYGLOT_TTS_CACHE_DIR") or "")
        self.cache_bytes = max(1, int(os.getenv("POLYGLOT_TTS_CACHE_MB") or "200")) * 1024 * 1024
        self._writes = 0
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
            except OSError as exc:
                LOG.warning("Speech cache directory %s is not usable (%s); clips are kept in memory only", self.cache_dir, exc)
                self.cache_dir = ""
        self.quota = QuotaTracker(os.path.join(self.cache_dir, "quota.json") if self.cache_dir else None)

    @staticmethod
    def models() -> list[str]:
        model = os.getenv("GEMINI_TTS_MODEL") or DEFAULT_MODEL
        return [model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [])

    @staticmethod
    def voice_for(lang: str, requested: str | None = None) -> str:
        if requested:
            match = next((v for v in VOICES if v.lower() == str(requested).strip().lower()), None)
            if not match:
                raise TtsError(f"Unknown voice {requested!r}", 400)
            return match
        return os.getenv(f"GEMINI_TTS_VOICE_{lang.upper()}") or os.getenv("GEMINI_TTS_VOICE") or DEFAULT_VOICE

    def quota_report(self) -> dict:
        return self.quota.report(self.models())

    # ---- the disk cache --------------------------------------------------------------------------

    def _disk_path(self, key: tuple[str, str, str]) -> str:
        digest = hashlib.sha256("\u241f".join(key).encode("utf-8")).hexdigest()[:40]
        return os.path.join(self.cache_dir, f"{key[0]}-{digest}.wav")

    def _disk_get(self, key) -> bytes | None:
        if not self.cache_dir:
            return None
        path = self._disk_path(key)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            os.utime(path)                     # recently heard clips are the last to be pruned
            return data if data[:4] == b"RIFF" else None
        except OSError:
            return None

    def _disk_put(self, key, wav: bytes) -> None:
        if not self.cache_dir:
            return
        path = self._disk_path(key)
        try:
            temporary = path + ".tmp"
            with open(temporary, "wb") as handle:
                handle.write(wav)
            os.replace(temporary, path)
            self._writes += 1
            if self._writes % 20 == 0:
                self._prune()
        except OSError as exc:
            LOG.debug("Could not keep a speech clip on disk: %s", exc)

    def _prune(self) -> None:
        clips = []
        for name in os.listdir(self.cache_dir):
            if name.endswith(".wav"):
                full = os.path.join(self.cache_dir, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                clips.append((stat.st_mtime, stat.st_size, full))
        total = sum(size for _, size, _ in clips)
        for _, size, full in sorted(clips):
            if total <= self.cache_bytes:
                break
            try:
                os.remove(full)
                total -= size
            except OSError:
                pass

    # ---- speaking --------------------------------------------------------------------------------

    def synthesize(self, text: str, lang: str, voice: str | None = None) -> bytes:
        text = " ".join(text.split())
        if not text:
            raise TtsError("There is no text to speak", 400)
        if len(text) > MAX_TEXT_CHARS:
            raise TtsError(f"Text must be {MAX_TEXT_CHARS} characters or fewer", 400)
        if lang not in LANGUAGE_CODES:
            raise TtsError(f"Unknown language {lang!r}", 400)
        voice = self.voice_for(lang, voice)
        model = os.getenv("GEMINI_TTS_MODEL") or DEFAULT_MODEL
        key = (lang, text, f"{model}/{voice}")
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        wav = self._disk_get(key)
        if wav is None:
            api_key = _api_key(lang)
            if not api_key:
                raise TtsError("Speech is not available: no Gemini API key is set", 503)
            with self._slots:
                wav = self._call(api_key, model, voice, text, lang)
            self._disk_put(key, wav)
        with self._lock:
            self._cache[key] = wav
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return wav

    def _call(self, api_key: str, model: str, voice: str, text: str, lang: str) -> bytes:
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as exc:
            raise TtsError("Speech needs the google-genai package", 503) from exc
        client = genai.Client(api_key=api_key, http_options={"timeout": 60000})
        models = [model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [])
        failures: list[str] = []
        busy = daily = False
        for candidate in models:
            if not self.quota.allowed(candidate):
                failures.append(f"{candidate}: today's allowance is used up")
                busy = daily = True
                continue
            try:
                self.quota.record(candidate)
                return self._try_model(client, types, candidate, voice, text, lang)
            except TtsError as exc:
                failures.append(f"{candidate}: {exc}")
                busy = busy or exc.status == 429
                if exc.status == 429 and re.search(r"PerDay|per day|daily", str(exc), re.IGNORECASE):
                    daily = True
                    self.quota.exhaust(candidate)
        LOG.warning("Speech failed on every model: %s", "; ".join(failures))
        if daily:
            raise TtsError("The daily speech quota for this API key is used up; it resets at midnight Pacific time", 429)
        if busy:
            raise TtsError("Speech is busy right now (rate limit); try again in a minute", 429)
        raise TtsError("The speech service returned no audio for this text", 502)

    def _try_model(self, client, types, model: str, voice: str, text: str, lang: str) -> bytes:
        def request(with_language: bool):
            speech = types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)),
                **({"language_code": LANGUAGE_CODES[lang]} if with_language else {}))
            return client.models.generate_content(
                model=model, contents=text,
                config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))

        last = "no attempt was made"
        status = 502
        for attempt, with_language in enumerate((True, False), 1):
            try:
                return self._audio_from(request(with_language))
            except TtsError as exc:                  # the model answered, but with no audio
                last, status = str(exc), exc.status
                break
            except Exception as exc:
                last = str(exc)
                LOG.info("Speech request to %s failed (language hint %s): %s", model, with_language, last[:140])
                if "INVALID_ARGUMENT" in last and with_language:
                    continue                          # this model does not take the language tag: try without it
                if "429" in last or "RESOURCE_EXHAUSTED" in last:
                    status = 429
                break
        raise TtsError(last, status)

    @staticmethod
    def _audio_from(response) -> bytes:
        try:
            part = response.candidates[0].content.parts[0]
            data = part.inline_data.data
        except (AttributeError, IndexError, TypeError):
            raise TtsError("The speech service returned no audio", 502) from None
        if not data:
            raise TtsError("The speech service returned no audio", 502)
        if data[:4] == b"RIFF":
            return data
        return pcm_to_wav(data)


SYNTHESIZER = SpeechSynthesizer()
