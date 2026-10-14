"""Text-to-speech for captions, using Gemini's speech models.

A caption can be read aloud in its own language: the dashboard's speaker buttons and the per-language
listener pages call POST /api/tts, which returns a WAV. Gemini returns raw 24 kHz 16-bit mono PCM (or, on
some models, a ready WAV); both are returned as a playable WAV. Results are kept in a small LRU so a phrase
heard twice costs one call.
"""
from __future__ import annotations

import io
import logging
import os
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


class SpeechSynthesizer:
    """Thread-safe: at most `concurrency` calls at once, and a small cache of finished clips."""

    def __init__(self, concurrency: int = 2) -> None:
        self._cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(concurrency)

    def synthesize(self, text: str, lang: str) -> bytes:
        text = " ".join(text.split())
        if not text:
            raise TtsError("There is no text to speak", 400)
        if len(text) > MAX_TEXT_CHARS:
            raise TtsError(f"Text must be {MAX_TEXT_CHARS} characters or fewer", 400)
        if lang not in LANGUAGE_CODES:
            raise TtsError(f"Unknown language {lang!r}", 400)
        voice = os.getenv("GEMINI_TTS_VOICE") or DEFAULT_VOICE
        model = os.getenv("GEMINI_TTS_MODEL") or DEFAULT_MODEL
        key = (lang, text, f"{model}/{voice}")
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        api_key = _api_key(lang)
        if not api_key:
            raise TtsError("Speech is not available: no Gemini API key is set", 503)
        with self._slots:
            wav = self._call(api_key, model, voice, text, lang)
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
            try:
                return self._try_model(client, types, candidate, voice, text, lang)
            except TtsError as exc:
                failures.append(f"{candidate}: {exc}")
                busy = busy or exc.status == 429
                daily = daily or (exc.status == 429 and "PerDay" in str(exc))
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
