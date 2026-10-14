"""Whisper speech-to-text producer with automatic leader rediscovery."""

from __future__ import annotations

import argparse
import base64
import logging
import os
import pathlib
import random
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

from common import config
from common.discovery import connect_to_leader
from common.protocol import ProtocolError, decode_stream, encode

LOG = logging.getLogger("polyglot.stt")

DEMO_SENTENCES = [
    "Good morning everyone",
    "Welcome to the multilingual meeting",
    "Please remain calm and follow the instructions",
    "The meeting starts now",
]


# How many un-acknowledged chunks a producer remembers to resend after a reconnect, and for how long.
UNACKED_MAX = 300
UNACKED_MAX_AGE = 120.0


class TranscriptProducer:
    """Sends transcript chunks to the leader broker and keeps each one until the broker acknowledges it, so a
    leader that dies after reading a chunk (but before routing and mirroring it) costs nothing: the chunk is
    sent again to the new leader, which ignores it if it already has it."""

    def __init__(self) -> None:
        self.sock: Any = None
        # A chunk may be sent from two threads (see web_ui's two-stage send);
        # one frame must never be interleaved with another on the socket.
        self._send_lock = threading.Lock()
        self._unacked: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._unacked_lock = threading.Lock()
        self._closed = False

    def _connect(self) -> bool:
        """Make sure there is a connection. True when this call made a new one: everything still
        un-acknowledged (including a chunk being sent right now) was resent on it."""
        if self.sock is not None:
            return False
        while self.sock is None:
            if self._closed:
                return False
            try:
                sock, response = connect_to_leader({"type": "register", "role": "stt",
                                                    "worker_id": f"stt-{uuid.uuid4().hex[:8]}"})
            except ConnectionError as exc:
                LOG.warning("No leader available (%s); retrying", exc)
                time.sleep(config.RECONNECT_DELAY)
                continue
            LOG.info("Connected to leader broker %s", response.get("leader_id"))
            with self._unacked_lock:
                pending = [message for _, message in self._unacked.values()]
            try:
                for message in pending:
                    sock.sendall(encode(message))
            except OSError as exc:
                LOG.warning("Resend after reconnecting failed (%s); rediscovering leader", exc)
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            self.sock = sock
            threading.Thread(target=self._read_acks, args=(sock,), name="stt-acks", daemon=True).start()
        return True

    def _read_acks(self, sock: Any) -> None:
        try:
            for message in decode_stream(sock):
                if message["type"] == "chunk_ack":
                    with self._unacked_lock:
                        self._unacked.pop(str(message.get("ack_id")), None)
        except (OSError, ProtocolError):
            pass
        except Exception:  # not readable at all (a test double): nothing more to learn from it
            return
        self._connection_lost(sock)

    def _connection_lost(self, sock: Any) -> None:
        """The leader hung up. Reconnect now if chunks are still waiting for their acknowledgement, rather than
        at the next send: a chunk written into a dead connection would otherwise vanish silently."""
        with self._send_lock:
            if self._closed or self.sock is not sock:
                return
            try:
                sock.close()
            except OSError:
                pass
            self.sock = None
            if self.unacknowledged():
                LOG.warning("Leader connection lost with %d chunk(s) unacknowledged; reconnecting", self.unacknowledged())
                self._connect()

    def _remember(self, ack_id: str, message: dict) -> None:
        now = time.monotonic()
        with self._unacked_lock:
            self._unacked[ack_id] = (now, message)
            while len(self._unacked) > UNACKED_MAX or (
                    self._unacked and now - next(iter(self._unacked.values()))[0] > UNACKED_MAX_AGE):
                self._unacked.popitem(last=False)

    def unacknowledged(self) -> int:
        with self._unacked_lock:
            return len(self._unacked)

    def send(self, text: str, chunk_id: str, audio_pcm: bytes | None = None,
             audio_rate: int = 16000, source_lang: str | None = None,
             pivot_en: str | None = None, targets: list[str] | None = None) -> None:
        ack_id = uuid.uuid4().hex[:12]
        message = {"type": "transcript_chunk", "chunk_id": chunk_id, "ack_id": ack_id,
                   "text": text, "timestamp": time.time()}
        if audio_pcm:
            message["audio_pcm_b64"] = base64.b64encode(audio_pcm).decode("ascii")
            message["audio_rate"] = audio_rate
        if source_lang:
            message["source_lang"] = source_lang
        if pivot_en:
            message["pivot_en"] = pivot_en  # one shared English rendering for workers to reuse
        if targets is not None:
            message["targets"] = list(targets)  # route to only these languages
        with self._send_lock:
            self._remember(ack_id, message)
            while True:
                if self._connect():
                    return                  # a new connection resent everything un-acknowledged, this chunk included
                try:
                    self.sock.sendall(encode(message))
                    return
                except OSError as exc:
                    LOG.warning("Send failed (%s); rediscovering leader", exc)
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None

    def close(self) -> None:
        self._closed = True
        if self.sock:
            self.sock.close()
            self.sock = None


_WHISPER_CACHE: dict[str, tuple[Any, Any]] = {}
_WHISPER_CACHE_LOCK = threading.Lock()


def _import_whisper():
    try:
        import whisper  # type: ignore
    except ImportError as exc:
        raise SystemExit("Whisper is not installed. Install requirements-stt.txt, or use --stdin.") from exc
    return whisper


def _load_whisper(model_name: str, need_model: bool = True):
    # Cached across calls in-process: the CLI only ever calls this once, but
    # web_ui.py's dashboard upload endpoint calls it once per request, and a
    # model load takes several seconds - reusing it matters there.
    # need_model=False returns (whisper module, None): decoding audio needs only
    # the module, so Burmese/Khmer/Lao (transcribed by Gemini) skip the ~20s model
    # load - and do not queue behind another thread that is loading it.
    if not need_model:
        cached = _WHISPER_CACHE.get(model_name)
        return cached if cached is not None else (_import_whisper(), None)
    with _WHISPER_CACHE_LOCK:
        cached = _WHISPER_CACHE.get(model_name)
        if cached is not None:
            return cached
        whisper = _import_whisper()
        LOG.info("Loading Whisper model %s", model_name)
        loaded = (whisper, whisper.load_model(model_name))
        _WHISPER_CACHE[model_name] = loaded
        return loaded


def _pcm16(audio: Any) -> bytes:
    """Convert Whisper/sounddevice float audio to little-endian PCM16."""
    return (audio.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()


# Confirmed by direct testing (not a guess): Whisper cannot reliably
# transcribe these three languages at any practical model size - `tiny`
# produces confidently-wrong text in the wrong script entirely, and `small`
# has such low confidence the hallucination filter drops it to nothing.
# Gemini's own audio understanding, by contrast, transcribed the same
# sample correctly (same model already used for translation on these three
# languages) - so for exactly these languages, prefer sending the audio to
# Gemini directly over trusting local Whisper at all. Only applies when the
# caller already knows the source language (a concrete code, not "auto");
# there is no way to pick this backend before transcribing unknown audio.
_GEMINI_STT_LANGUAGES = frozenset({"my", "km", "lo"})


def gemini_first(language: str | None) -> bool:
    """Should this (known) spoken language be transcribed by Gemini rather than Whisper?

    Always for Burmese, Khmer and Lao. For the rest it is POLYGLOT_GEMINI_STT_LANGUAGES: "all" (the
    default; an empty value means the default), "none", or a comma list such as "my,km,lo,vi". Measured
    on real FLEURS speech, Gemini made 2-7x fewer character errors than Whisper `base` in every language
    and was faster per clip; live Vietnamese through Whisper dropped 26 of 29 slices and returned nonsense."""
    if language is None:
        return False
    if language in _GEMINI_STT_LANGUAGES:
        return True
    setting = (os.getenv("POLYGLOT_GEMINI_STT_LANGUAGES") or "all").strip().lower()
    if setting == "all":
        return language in config.TARGET_LANGUAGES
    if setting == "none":
        return False
    return language in {code.strip() for code in setting.split(",")}


# Whisper runs on the CPU and one inference already uses every core: when Gemini fails for a language
# Whisper can read, the fallback transcriptions must not pile up on top of each other.
_WHISPER_FALLBACK_LOCK = threading.Lock()


def _pcm_window_to_wav_bytes(window: Any, sample_rate: int) -> bytes:
    """Wrap a Whisper-loaded float32 audio window as an in-memory 16-bit PCM
    WAV file - the format Gemini's generateContent audio input expects."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(_pcm16(window))
    return buf.getvalue()


# A window shorter than this, or with no signal at all, holds nothing worth
# sending: asking a speech model to transcribe a sliver of audio is how
# invented words get into a caption.
_MIN_WINDOW_SECONDS = 0.7
# Peak level (0-1) below which a window is treated as no signal at all. Real, quiet read-aloud
# speech in FLEURS reaches peaks of about 0.002, so this sits well under that: an earlier value of
# 0.003 silently dropped 7 of 1,350 real recordings. Digital silence and a muted microphone are ~0.
_SILENCE_PEAK = 0.001


def _window_is_unusable(window: Any, sample_rate: int) -> bool:
    """True for a window too short, or too silent, to transcribe. Errs towards
    sending: anything it cannot measure counts as usable."""
    try:
        minimum = float(os.getenv("POLYGLOT_MIN_WINDOW_SECONDS") or _MIN_WINDOW_SECONDS)
        if len(window) < minimum * sample_rate:
            return True
        floor = float(os.getenv("POLYGLOT_SILENCE_PEAK") or _SILENCE_PEAK)
        return float(abs(window).max()) < floor
    except Exception:
        return False


# Longest we will wait between Gemini transcription retries. A live slice is
# only a few seconds long, so a long back-off is worse than a gap.
_GEMINI_STT_MAX_BACKOFF = 6.0


def _transcribe_window_via_gemini(window: Any, sample_rate: int, language: str) -> str | None:
    """Send one audio window straight to Gemini for transcription.

    Returns the transcript - "" when the window holds no speech - or None
    when Gemini could not be reached (no key, SDK missing, or the call still
    failing after bounded retries). None means "could not transcribe", and
    the caller must NOT substitute Whisper for my/km/lo: it cannot recognise
    them, and its fluent-looking nonsense would be translated into every
    other language. Transient errors (503 overloaded, 429 rate limit,
    dropped connections) are retried GEMINI_STT_MAX_RETRIES times (default
    2) with a short capped back-off; a daily quota or a retry hint longer
    than the cap is not worth waiting for. Never raises."""
    api_key = (
        os.getenv(f"GEMINI_API_KEY_{language.upper()}")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )
    if not api_key:
        LOG.warning("No Gemini API key set; cannot transcribe a %s window", language)
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        from translator_worker import _is_daily_quota_exhausted, _is_retryable, _suggested_retry_delay

        client = genai.Client(api_key=api_key, http_options={"timeout": 60000})
        model = os.getenv("GEMINI_STT_MODEL") or os.getenv("GEMINI_TEXT_MODEL") or "gemini-flash-latest"
        wav_bytes = _pcm_window_to_wav_bytes(window, sample_rate)
        contents = [types.Content(parts=[
            types.Part(text=(
                f"Transcribe the {config.language_label(language)} speech in this audio "
                "exactly, in its original script. Return only the transcription, with no "
                "explanation, labels, or quotes. If the audio holds no clear speech, return "
                "nothing at all - never guess or invent words."
            )),
            types.Part(inline_data=types.Blob(data=wav_bytes, mime_type="audio/wav")),
        ])]
    except Exception as exc:
        LOG.warning("Gemini transcription unavailable for %s (%s)", language, exc)
        return None

    retries = max(0, int(os.getenv("GEMINI_STT_MAX_RETRIES") or "2"))
    for attempt in range(retries + 1):
        try:
            resp = client.models.generate_content(model=model, contents=contents)
            return (resp.text or "").strip()
        except Exception as exc:
            suggested = _suggested_retry_delay(exc)
            hopeless = (_is_daily_quota_exhausted(exc) or not _is_retryable(exc)
                        or (suggested is not None and suggested > _GEMINI_STT_MAX_BACKOFF))
            if attempt == retries or hopeless:
                LOG.warning("Gemini transcription failed for %s window (%s)", language, exc)
                return None
            delay = min(1.0 * 2 ** attempt + random.uniform(0, 1.0), _GEMINI_STT_MAX_BACKOFF)
            if suggested is not None:
                delay = max(delay, suggested)
            LOG.warning("Gemini transcription for %s hit a transient error (%s); retry %s/%s in %.1fs",
                        language, exc, attempt + 1, retries, delay)
            time.sleep(delay)
    return None  # pragma: no cover - the loop always returns


def _transcribe_window_via_gemini_auto(window: Any, sample_rate: int) -> tuple[str, str] | None:
    """Recovery path for "Auto-detect" (language=None): triggered when
    Whisper's own detected language lands outside our 9 supported codes
    entirely - the exact signature both confirmed real Burmese failures
    showed (Whisper mis-detected the language completely rather than
    merely transcribing it poorly). A caller-specified language already
    routes my/km/lo through _transcribe_window_via_gemini above and never
    reaches here; this only covers the case where auto-detect itself picked
    the wrong language before transcription could even start. Since we
    don't know which language it actually is either, this asks Gemini to
    identify it from the same 9-language set and transcribe in one call.
    Returns None on any failure, so the caller keeps Whisper's (wrong) text
    rather than losing the chunk entirely."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        client = genai.Client(api_key=api_key, http_options={"timeout": 60000})
        model = os.getenv("GEMINI_STT_MODEL") or os.getenv("GEMINI_TEXT_MODEL") or "gemini-flash-latest"
        wav_bytes = _pcm_window_to_wav_bytes(window, sample_rate)
        options = ", ".join(f"{code}={config.language_label(code)}" for code in config.TARGET_LANGUAGES)
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(parts=[
                types.Part(text=(
                    f"Identify which of these languages is spoken in this audio: {options}. "
                    "Then transcribe the speech exactly, in its original script. Reply with "
                    "exactly two lines and nothing else:\nLANGUAGE: <matching code from the list>\n"
                    "TEXT: <the transcription>"
                )),
                types.Part(inline_data=types.Blob(data=wav_bytes, mime_type="audio/wav")),
            ])],
        )
        reply = (resp.text or "").strip()
        lang_match = re.search(r"LANGUAGE:\s*(\w+)", reply)
        text_match = re.search(r"TEXT:\s*(.+)", reply, re.DOTALL)
        if not lang_match or not text_match:
            return None
        lang = lang_match.group(1).strip().lower()
        text = text_match.group(1).strip()
        if lang not in config.TARGET_LANGUAGES or not text:
            return None
        return text, lang
    except Exception as exc:
        LOG.warning("Gemini auto-detect recovery failed (%s); keeping Whisper's result", exc)
        return None


def _detect_text_language(text: str) -> tuple[str, float] | None:
    """Lightweight text-based language ID (py3langid - a small, self-
    contained statistical classifier, no network download beyond pip
    install) as a second signal alongside Whisper's own audio-level
    confidence scores (no_speech_prob/avg_logprob below). It catches a
    different failure mode: text Whisper transcribed fluently and
    confidently, but in the wrong language entirely - the model is
    "confident" in the words it chose, just not about which language it
    actually is, which audio-level confidence alone doesn't always catch.
    Lazily imported (like whisper itself in _load_whisper) so importing
    this module doesn't require it - the lightweight `mock`/`translate`
    images, and the test suite built on `mock`, never install it."""
    try:
        import py3langid as langid  # type: ignore
    except ImportError:
        return None
    code, score = langid.classify(text)
    return code, score


def transcribe_file(path: pathlib.Path, model_name: str, language: str | None = None,
                    on_unrecognized: Callable[[], None] | None = None) -> Iterator[tuple[str, bytes, str | None]]:
    """Decode media once; yield one transcript per short inference window.

    on_unrecognized is called once for each window of a Whisper-incapable
    language (my/km/lo) that Gemini could not transcribe; that window is
    skipped, leaving a gap rather than Whisper's nonsense.

    Translation can start before later windows have been transcribed.
    Windows may split sentences; 15 seconds balances context and latency.
    Audio decoding still loads the file; this is incremental inference,
    not a streaming media decoder.
    """
    use_gemini = gemini_first(language)
    whisper, model = _load_whisper(model_name, need_model=not use_gemini)
    audio = whisper.load_audio(str(path))
    sample_rate = whisper.audio.SAMPLE_RATE
    # condition_on_previous_text=False: Whisper's default feeds each
    # segment's decoding the previous segment's text as context, so one
    # hallucinated segment (e.g. from music/noise/silence, or a confident
    # wrong guess on non-English audio) can cascade into further
    # hallucinated segments that "continue" the fabricated text. Off
    # trades a little cross-segment coherence for not compounding errors.
    # language=None (the "auto" caller choice) lets Whisper detect it as
    # before; a concrete code (the caller already knows what's spoken, same
    # "source_lang" the caption is tagged with downstream) skips detection
    # entirely - both faster and more accurate than guessing, particularly
    # for whichever of the 9 languages Whisper is weaker at recognizing
    # unprompted.
    window_seconds = max(5, min(60, float(os.getenv("POLYGLOT_FILE_CHUNK_SECONDS", "15"))))
    window_samples = int(window_seconds * sample_rate)
    for offset in range(0, len(audio), window_samples):
        window = audio[offset:offset + window_samples]

        # Whisper is unreliable for these three regardless of model size
        # (confirmed by direct testing - see _GEMINI_STT_LANGUAGES above);
        # use Gemini's own audio transcription. If it is unavailable, leave a
        # gap: Whisper's attempt at these languages is fluent-looking
        # nonsense that would then be translated into eight other languages.
        use_whisper_fallback = False
        if use_gemini:
            if _window_is_unusable(window, sample_rate):
                continue  # silence, not a failure: nothing to transcribe, nothing missed
            gemini_text = _transcribe_window_via_gemini(window, sample_rate, language)
            if gemini_text is not None:
                if gemini_text:
                    yield gemini_text, _pcm16(window), language
                continue
            if language in _GEMINI_STT_LANGUAGES:
                LOG.warning("Skipping a %s window: Gemini transcription unavailable", language)
                if on_unrecognized:
                    on_unrecognized()
                continue
            # Whisper can read this language, so an unavailable Gemini costs accuracy, not the caption.
            LOG.warning("Gemini transcription unavailable for a %s window; using Whisper for it", language)
            use_whisper_fallback = True
            if model is None:
                whisper, model = _load_whisper(model_name)

        if use_whisper_fallback:
            with _WHISPER_FALLBACK_LOCK:
                result = model.transcribe(window, fp16=False, condition_on_previous_text=False, language=language)
        else:
            result = model.transcribe(window, fp16=False, condition_on_previous_text=False, language=language)
        detected = result.get("language")
        pieces = []
        for segment in result.get("segments", []):
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            if segment.get("no_speech_prob", 0.0) > 0.6 and segment.get("avg_logprob", 0.0) < -1.0:
                continue
            pieces.append(text)
        text = " ".join(pieces)

        # In auto-detect mode, Whisper's language-ID and its actual decoder
        # can disagree: confirmed by direct testing, Whisper correctly
        # identified a Burmese sample as 'my' while its decoder still
        # produced complete gibberish (mixed English/Chinese/Korean
        # fragments) - the language head alone isn't a reliable enough
        # signal that the transcription itself is trustworthy. Two cases:
        # detected lands on one of the three known-weak languages (trust
        # the language-ID, route to the same direct per-language Gemini
        # path used when the caller specifies it up front); or detected
        # falls outside our 9 supported codes entirely (Whisper got the
        # language wrong too - ask Gemini to identify and transcribe from
        # scratch). A caller-specified language never reaches here.
        if language is None:
            if detected in _GEMINI_STT_LANGUAGES:
                gemini_text = _transcribe_window_via_gemini(window, sample_rate, detected)
                if gemini_text is None:
                    text = ""  # Whisper's own text for this language is unreliable
                    if on_unrecognized:
                        on_unrecognized()
                else:
                    text = gemini_text
            elif detected not in config.TARGET_LANGUAGES:
                recovered = _transcribe_window_via_gemini_auto(window, sample_rate)
                if recovered:
                    text, detected = recovered

        if text:
            yield text, _pcm16(window), detected


def transcribe_microphone(model_name: str, language: str | None = None) -> Iterator[tuple[str, bytes]]:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:
        raise SystemExit("sounddevice is not installed; install requirements-stt.txt") from exc
    whisper, model = _load_whisper(model_name)
    sample_rate = whisper.audio.SAMPLE_RATE
    frame_count = int(sample_rate * config.AUDIO_CHUNK_SECONDS)
    LOG.info("Recording %.1fs microphone chunks; press Ctrl+C to stop", config.AUDIO_CHUNK_SECONDS)
    while True:
        recording = sd.rec(frame_count, samplerate=sample_rate, channels=1, dtype="float32")
        sd.wait()
        result = model.transcribe(recording.reshape(-1), fp16=False, language=language)
        text = str(result.get("text", "")).strip()
        if text:
            yield text, _pcm16(recording.reshape(-1))


def stdin_transcripts() -> Iterator[str]:
    LOG.info("Reading one transcript chunk per line from stdin")
    for line in sys.stdin:
        text = line.strip()
        if text:
            yield text


def demo_transcripts(interval: float) -> Iterator[str]:
    """Generate a repeating local transcript stream for one-command demos."""
    LOG.info("Starting repeating demo transcript stream")
    time.sleep(2.0)  # allow listeners to finish registering
    while True:
        for sentence in DEMO_SENTENCES:
            yield sentence
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input-file", type=pathlib.Path, help="WAV/audio file instead of microphone")
    source.add_argument("--stdin", action="store_true", help="read transcript text from stdin (no Whisper needed)")
    source.add_argument("--demo", action="store_true", help="continuously send built-in test captions")
    parser.add_argument("--demo-interval", type=float, default=3.0, help="seconds between demo captions")
    parser.add_argument("--model", default="base", help="Whisper model name (use tiny on slower CPUs, small for better accuracy on a fast connection)")
    parser.add_argument("--send-audio", action="store_true",
                        help="also attach raw PCM so workers can use Gemini Live speech-to-speech "
                             "translation instead of the text pipeline. Off by default: translating "
                             "the Whisper transcript as text is faster, cheaper, and more reliable "
                             "across many target languages than one live audio session per chunk.")
    parser.add_argument("--source-lang", default="auto", choices=["auto", *config.TARGET_LANGUAGES],
                        help="language the speaker is using. 'auto' (default) lets each worker's "
                             "Gemini call detect it. Passing a concrete code lets the worker whose "
                             "language matches skip translation entirely (a free accuracy + speed win).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.input_file and not args.input_file.exists():
        parser.error(f"input file does not exist: {args.input_file}")
    lang_hint = args.source_lang if args.source_lang != "auto" else None
    transcripts = demo_transcripts(args.demo_interval) if args.demo else (
        stdin_transcripts() if args.stdin else (
            transcribe_file(args.input_file, args.model, language=lang_hint) if args.input_file
            else transcribe_microphone(args.model, language=lang_hint)))
    producer = TranscriptProducer()
    try:
        for sequence, item in enumerate(transcripts, 1):
            if isinstance(item, tuple):
                text, audio_pcm = item[0], item[1]
                detected_lang = item[2] if len(item) > 2 else None
            else:
                text, audio_pcm, detected_lang = item, None, None
            # A per-chunk language Whisper actually detected (transcribe_file
            # only, when --source-lang was left at "auto") is more useful
            # downstream than the literal string "auto" - it lets the
            # matching worker passthrough and every other worker skip its
            # own detection step. An explicit --source-lang always wins
            # over it regardless, matching the flag's documented meaning.
            effective_source_lang = args.source_lang
            if effective_source_lang == "auto" and detected_lang in config.LANGUAGES:
                effective_source_lang = detected_lang
            chunk_id = f"{int(time.time() * 1000)}-{sequence}"
            print(f"[{chunk_id}] {text}", flush=True)
            producer.send(text, chunk_id, audio_pcm=audio_pcm if args.send_audio else None,
                          source_lang=effective_source_lang)
    except KeyboardInterrupt:
        pass
    finally:
        producer.close()


if __name__ == "__main__":
    main()
