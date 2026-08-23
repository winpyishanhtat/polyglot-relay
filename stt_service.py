"""Whisper speech-to-text producer with automatic leader rediscovery."""

from __future__ import annotations

import argparse
import base64
import logging
import pathlib
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from common import config
from common.discovery import connect_to_leader
from common.protocol import encode

LOG = logging.getLogger("polyglot.stt")

DEMO_SENTENCES = [
    "Good morning everyone",
    "Welcome to the multilingual meeting",
    "Please remain calm and follow the instructions",
    "The meeting starts now",
]


class TranscriptProducer:
    def __init__(self) -> None:
        self.sock: Any = None

    def _connect(self) -> None:
        while self.sock is None:
            try:
                self.sock, response = connect_to_leader({"type": "register", "role": "stt",
                                                         "worker_id": f"stt-{uuid.uuid4().hex[:8]}"})
                LOG.info("Connected to leader broker %s", response.get("leader_id"))
            except ConnectionError as exc:
                LOG.warning("No leader available (%s); retrying", exc)
                time.sleep(config.RECONNECT_DELAY)

    def send(self, text: str, chunk_id: str, audio_pcm: bytes | None = None,
             audio_rate: int = 16000, source_lang: str | None = None) -> None:
        message = {"type": "transcript_chunk", "chunk_id": chunk_id,
                   "text": text, "timestamp": time.time()}
        if audio_pcm:
            message["audio_pcm_b64"] = base64.b64encode(audio_pcm).decode("ascii")
            message["audio_rate"] = audio_rate
        if source_lang:
            message["source_lang"] = source_lang
        while True:
            self._connect()
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
        if self.sock:
            self.sock.close()
            self.sock = None


_WHISPER_CACHE: dict[str, tuple[Any, Any]] = {}
_WHISPER_CACHE_LOCK = threading.Lock()


def _load_whisper(model_name: str):
    # Cached across calls in-process: the CLI only ever calls this once, but
    # web_ui.py's dashboard upload endpoint calls it once per request, and a
    # model load takes several seconds - reusing it matters there.
    with _WHISPER_CACHE_LOCK:
        cached = _WHISPER_CACHE.get(model_name)
        if cached is not None:
            return cached
        try:
            import whisper  # type: ignore
        except ImportError as exc:
            raise SystemExit("Whisper is not installed. Install requirements-stt.txt, or use --stdin.") from exc
        LOG.info("Loading Whisper model %s", model_name)
        loaded = (whisper, whisper.load_model(model_name))
        _WHISPER_CACHE[model_name] = loaded
        return loaded


def _pcm16(audio: Any) -> bytes:
    """Convert Whisper/sounddevice float audio to little-endian PCM16."""
    return (audio.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()


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


def transcribe_file(path: pathlib.Path, model_name: str, language: str | None = None) -> Iterator[tuple[str, bytes, str | None]]:
    # One whole-file pass, using Whisper's own internal segmentation, rather
    # than pre-slicing into fixed AUDIO_CHUNK_SECONDS windows: slicing first
    # cuts sentences mid-word at arbitrary boundaries (Whisper then often
    # returns empty text for the fragment, silently dropping that part of
    # the audio) and pays a full padded-to-30s encoder pass per slice
    # instead of per ~30s of real audio - both much worse for a finished
    # recording than for live streaming, which is what that fixed window is
    # actually tuned for (see transcribe_microphone below).
    whisper, model = _load_whisper(model_name)
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
    result = model.transcribe(audio, fp16=False, condition_on_previous_text=False, language=language)
    # Whisper always reports back what language it used, whether given
    # explicitly (language echoes the same value back) or detected itself
    # (auto mode) - propagating this to the caller means an "Auto-detect"
    # upload can still get the passthrough shortcut and the known-source
    # Gemini prompt variant downstream, instead of staying tagged "auto"
    # (and every worker guessing) just because the user didn't type it in.
    detected_language = result.get("language")
    for segment in result.get("segments", []):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        # Whisper's own confidence signals: no_speech_prob high means it
        # thinks this span probably isn't speech at all (silence/music/
        # noise) - a classic hallucination trigger. avg_logprob very low
        # means it wasn't confident in the words it chose either way.
        # Same thresholds Whisper's CLI uses to decide "probably bogus".
        if segment.get("no_speech_prob", 0.0) > 0.6 and segment.get("avg_logprob", 0.0) < -1.0:
            continue
        # Second, independent hallucination signal: a text-based language
        # ID confidently disagreeing with what Whisper thinks it just
        # transcribed. Only acts on a *confident* disagreement (>0.9) -
        # short fragments are inherently ambiguous to a text classifier,
        # so a low-confidence guess isn't worth discarding real captions
        # over.
        expected_lang = language or detected_language
        text_id = _detect_text_language(text)
        if expected_lang and text_id and text_id[0] != expected_lang and text_id[1] > 0.9:
            continue
        start = max(0, int(segment["start"] * sample_rate))
        end = min(len(audio), int(segment["end"] * sample_rate))
        yield text, _pcm16(audio[start:end]), detected_language


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
