"""Speech-to-text producer: the broker client, Whisper windowing and filtering,
microphone/stdin/demo sources and the command line. Whisper, sounddevice and
py3langid are replaced with small fakes, so no model or hardware is needed."""

import io
import socket
import sys
import threading
import time
import types
import unittest
import wave
from unittest import mock

try:
    import numpy as np
except ImportError:  # the lightweight mock image is stdlib-only
    np = None

import stt_service
from common import config
from common.protocol import decode_stream


class FakeSock:
    def __init__(self, fail=False):
        self.sent, self.closed, self.fail = [], False, fail

    def sendall(self, data):
        if self.fail:
            raise OSError("broken pipe")
        self.sent.append(data)

    def close(self):
        self.closed = True


def frames(sock):
    """Decode every protocol frame a FakeSock received."""
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"".join(sock.sent))
        b.shutdown(socket.SHUT_WR)
        return list(decode_stream(a))


class TranscriptProducerTests(unittest.TestCase):
    def producer_with(self, *socks):
        producer = stt_service.TranscriptProducer()
        patcher = mock.patch.object(stt_service, "connect_to_leader",
                                    side_effect=[(s, {"leader_id": 1}) for s in socks])
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        return producer

    def test_connects_lazily_registering_as_an_stt_producer(self):
        sock = FakeSock()
        producer = self.producer_with(sock)
        self.connect.assert_not_called()
        producer.send("hello", "c1")
        registration = self.connect.call_args.args[0]
        self.assertEqual((registration["type"], registration["role"]), ("register", "stt"))
        self.assertTrue(registration["worker_id"].startswith("stt-"))

    def test_reuses_one_connection_for_many_chunks(self):
        sock = FakeSock()
        producer = self.producer_with(sock)
        for n in range(3):
            producer.send("hello", f"c{n}")
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual([f["chunk_id"] for f in frames(sock)], ["c0", "c1", "c2"])

    def test_minimal_chunk_has_only_the_required_fields(self):
        sock = FakeSock()
        self.producer_with(sock).send("hello", "c1")
        message = frames(sock)[0]
        self.assertEqual(set(message), {"type", "chunk_id", "text", "timestamp"})
        self.assertEqual(message["type"], "transcript_chunk")

    def test_optional_fields_are_attached_only_when_given(self):
        sock = FakeSock()
        self.producer_with(sock).send("hello", "c1", audio_pcm=b"\x01\x02", audio_rate=8000,
                                      source_lang="my", pivot_en="Hi", targets=["en", "th"])
        message = frames(sock)[0]
        self.assertEqual((message["audio_rate"], message["source_lang"], message["pivot_en"], message["targets"]),
                         (8000, "my", "Hi", ["en", "th"]))
        import base64
        self.assertEqual(base64.b64decode(message["audio_pcm_b64"]), b"\x01\x02")

    def test_an_empty_target_list_is_kept_because_it_means_send_to_nobody(self):
        sock = FakeSock()
        self.producer_with(sock).send("hello", "c1", targets=[])
        self.assertEqual(frames(sock)[0]["targets"], [])

    def test_a_dead_connection_is_replaced_and_the_chunk_resent(self):
        dead, fresh = FakeSock(fail=True), FakeSock()
        producer = self.producer_with(dead, fresh)
        producer.send("hello", "c1")
        self.assertTrue(dead.closed)
        self.assertEqual(frames(fresh)[0]["chunk_id"], "c1")
        self.assertEqual(self.connect.call_count, 2)

    def test_connection_failures_are_retried_after_a_pause(self):
        sock = FakeSock()
        producer = stt_service.TranscriptProducer()
        with mock.patch.object(stt_service, "connect_to_leader",
                               side_effect=[ConnectionError("none"), ConnectionError("none"), (sock, {"leader_id": 2})]) as connect, \
                mock.patch.object(stt_service.time, "sleep") as sleep:
            producer.send("hello", "c1")
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(frames(sock)), 1)

    def test_concurrent_senders_never_interleave_frames(self):
        sock = FakeSock()
        producer = self.producer_with(sock)
        threads = [threading.Thread(target=lambda n=n: [producer.send("x" * 500, f"t{n}-{i}") for i in range(20)])
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(frames(sock)), 80)
        self.assertTrue(all(chunk.endswith(b"\n") and chunk.count(b"\n") == 1 for chunk in sock.sent))

    def test_close_is_idempotent(self):
        sock = FakeSock()
        producer = self.producer_with(sock)
        producer.send("hello", "c1")
        producer.close()
        producer.close()
        self.assertTrue(sock.closed)
        self.assertIsNone(producer.sock)


@unittest.skipUnless(np is not None, "numpy is installed only in the full image")
class AudioHelperTests(unittest.TestCase):
    def test_pcm16_clips_and_scales_to_little_endian_16_bit(self):
        pcm = stt_service._pcm16(np.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype=np.float32))
        self.assertEqual(np.frombuffer(pcm, dtype="<i2").tolist(), [0, 16383, -16383, 32767, -32767])

    def test_window_becomes_a_valid_mono_16_bit_wav(self):
        samples = np.sin(np.linspace(0, 20, 1600)).astype(np.float32)
        data = stt_service._pcm_window_to_wav_bytes(samples, 16000)
        with wave.open(io.BytesIO(data)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()),
                             (1, 2, 16000, 1600))

    def test_text_language_detection_uses_py3langid_when_present(self):
        fake = types.SimpleNamespace(classify=lambda text: ("my", -120.5))
        with mock.patch.dict(sys.modules, {"py3langid": fake}):
            self.assertEqual(stt_service._detect_text_language("\u1019\u1004\u103a"), ("my", -120.5))


class FakeWhisper:
    """Replaces the whisper module: windows of 'speech' come back as scripted results."""
    SAMPLE_RATE = 16000

    def __init__(self, results, seconds=40):
        self.results, self.seconds = list(results), seconds
        self.audio = types.SimpleNamespace(SAMPLE_RATE=self.SAMPLE_RATE)
        self.transcribe_calls, self.loaded = [], []
        self.model = types.SimpleNamespace(transcribe=self._transcribe)

    def load_audio(self, path):
        return np.full(self.seconds * self.SAMPLE_RATE, 0.05, dtype=np.float32)   # faint but audible

    def load_model(self, name):
        self.loaded.append(name)
        return self.model

    def _transcribe(self, window, **kwargs):
        self.transcribe_calls.append((len(window), kwargs))
        return self.results.pop(0)


def seg(text, no_speech=0.0, logprob=-0.2):
    return {"text": text, "no_speech_prob": no_speech, "avg_logprob": logprob}


class WhisperLoaderTests(unittest.TestCase):
    def setUp(self):
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)

    def test_models_are_loaded_once_per_name(self):
        fake = FakeWhisper([])
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            first = stt_service._load_whisper("base")
            second = stt_service._load_whisper("base")
            stt_service._load_whisper("small")
        self.assertIs(first, second)
        self.assertEqual(fake.loaded, ["base", "small"])

    def test_missing_whisper_exits_with_an_install_hint(self):
        with mock.patch.dict(sys.modules, {"whisper": None}):
            with self.assertRaises(SystemExit) as ctx:
                stt_service._load_whisper("base")
        self.assertIn("Whisper is not installed", str(ctx.exception))


@unittest.skipUnless(np is not None, "numpy is installed only in the full image")
class TranscribeFileTests(unittest.TestCase):
    def setUp(self):
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)

    def run_file(self, fake, language=None, on_unrecognized=None, **env):
        stt_service._WHISPER_CACHE.clear()  # each fake whisper must be the one that gets loaded
        with mock.patch.dict(sys.modules, {"whisper": fake}), mock.patch.dict("os.environ", env):
            return list(stt_service.transcribe_file("clip.wav", "base", language, on_unrecognized))

    def test_one_transcript_per_fifteen_second_window(self):
        fake = FakeWhisper([{"language": "en", "segments": [seg(f"part {n}")]} for n in range(3)], seconds=40)
        out = self.run_file(fake)
        self.assertEqual([text for text, _, _ in out], ["part 0", "part 1", "part 2"])
        self.assertEqual([n for n, _ in fake.transcribe_calls], [15 * 16000, 15 * 16000, 10 * 16000])

    def test_window_length_is_configurable_but_clamped(self):
        for configured, expected in (("1", 5), ("20", 20), ("999", 60)):
            fake = FakeWhisper([{"language": "en", "segments": [seg("x")]}] * 30, seconds=120)
            self.run_file(fake, POLYGLOT_FILE_CHUNK_SECONDS=configured)
            self.assertEqual(fake.transcribe_calls[0][0], expected * 16000, configured)

    def test_each_transcript_carries_its_audio_and_detected_language(self):
        fake = FakeWhisper([{"language": "th", "segments": [seg("hello")]}], seconds=2)
        text, pcm, language = self.run_file(fake)[0]
        self.assertEqual((text, language, len(pcm)), ("hello", "th", 2 * 16000 * 2))

    def test_whisper_is_told_not_to_carry_text_between_windows(self):
        fake = FakeWhisper([{"language": "en", "segments": [seg("x")]}], seconds=2)
        self.run_file(fake)
        kwargs = fake.transcribe_calls[0][1]
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assertFalse(kwargs["fp16"])
        self.assertIsNone(kwargs["language"])

    def test_a_known_language_skips_whispers_detection(self):
        fake = FakeWhisper([{"language": "th", "segments": [seg("x")]}], seconds=2)
        self.run_file(fake, language="th")
        self.assertEqual(fake.transcribe_calls[0][1]["language"], "th")

    def test_segments_are_joined_and_blank_ones_dropped(self):
        fake = FakeWhisper([{"language": "en", "segments": [seg(" one "), seg("   "), seg("two")]}], seconds=2)
        self.assertEqual(self.run_file(fake)[0][0], "one two")

    def test_silence_hallucinations_need_both_signals_to_be_dropped(self):
        segments = [seg("hallucinated", no_speech=0.9, logprob=-1.5),   # both bad: dropped
                    seg("quiet but sure", no_speech=0.9, logprob=-0.3),  # only no_speech high: kept
                    seg("unsure but speech", no_speech=0.1, logprob=-2.0)]  # only logprob low: kept
        fake = FakeWhisper([{"language": "en", "segments": segments}], seconds=2)
        self.assertEqual(self.run_file(fake)[0][0], "quiet but sure unsure but speech")

    def test_a_window_with_nothing_recognised_yields_nothing(self):
        fake = FakeWhisper([{"language": "en", "segments": []}, {"language": "en", "segments": [seg("x", 0.9, -2)]}],
                           seconds=20)
        self.assertEqual(self.run_file(fake), [])

    def test_a_known_whisper_incapable_language_goes_to_gemini_not_whisper(self):
        fake = FakeWhisper([], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value="\u1019\u1004\u103a") as gem:
            out = self.run_file(fake, language="my")
        self.assertEqual((out[0][0], out[0][2]), ("\u1019\u1004\u103a", "my"))
        self.assertEqual(fake.transcribe_calls, [])
        self.assertEqual(gem.call_args.args[2], "my")

    def test_silence_from_gemini_is_skipped_without_counting_as_unrecognised(self):
        calls = []
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value=""):
            out = self.run_file(FakeWhisper([], seconds=2), language="km", on_unrecognized=lambda: calls.append(1))
        self.assertEqual((out, calls), ([], []))

    def test_gemini_failure_counts_as_unrecognised_and_leaves_a_gap(self):
        calls = []
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value=None):
            out = self.run_file(FakeWhisper([], seconds=40), language="lo", on_unrecognized=lambda: calls.append(1))
        self.assertEqual((out, len(calls)), ([], 3))

    def test_auto_detect_routes_a_detected_weak_language_to_gemini(self):
        fake = FakeWhisper([{"language": "my", "segments": [seg("gibberish")]}], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value="real burmese") as gem:
            out = self.run_file(fake)
        self.assertEqual((out[0][0], out[0][2]), ("real burmese", "my"))
        self.assertEqual(gem.call_args.args[2], "my")

    def test_auto_detect_never_keeps_whisper_text_when_gemini_fails_for_a_weak_language(self):
        calls = []
        fake = FakeWhisper([{"language": "km", "segments": [seg("gibberish")]}], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value=None):
            out = self.run_file(fake, on_unrecognized=lambda: calls.append(1))
        self.assertEqual((out, len(calls)), ([], 1))

    def test_auto_detect_outside_the_supported_set_asks_gemini_to_identify(self):
        fake = FakeWhisper([{"language": "zh", "segments": [seg("wrong script")]}], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini_auto", return_value=("\u1019", "my")):
            out = self.run_file(fake)
        self.assertEqual((out[0][0], out[0][2]), ("\u1019", "my"))

    def test_failed_recovery_keeps_whispers_text(self):
        fake = FakeWhisper([{"language": "zh", "segments": [seg("best effort")]}], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini_auto", return_value=None):
            out = self.run_file(fake)
        self.assertEqual((out[0][0], out[0][2]), ("best effort", "zh"))

    def test_supported_detected_language_needs_no_gemini_at_all(self):
        fake = FakeWhisper([{"language": "vi", "segments": [seg("xin chao")]}], seconds=2)
        with mock.patch.object(stt_service, "_transcribe_window_via_gemini", side_effect=AssertionError), \
                mock.patch.object(stt_service, "_transcribe_window_via_gemini_auto", side_effect=AssertionError):
            self.assertEqual(self.run_file(fake)[0][0], "xin chao")


@unittest.skipUnless(np is not None, "numpy is installed only in the full image")
class MicrophoneTests(unittest.TestCase):
    def setUp(self):
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)

    def fake_sounddevice(self):
        recorded = []

        def rec(frames, samplerate, channels, dtype):
            recorded.append((frames, samplerate, channels, dtype))
            return np.full((frames, 1), 0.25, dtype=np.float32)
        return types.SimpleNamespace(rec=rec, wait=lambda: None), recorded

    def test_records_fixed_chunks_and_yields_text_with_audio(self):
        sd, recorded = self.fake_sounddevice()
        fake = FakeWhisper([{"text": " hello "}, {"text": "  "}, {"text": "again"}])
        with mock.patch.dict(sys.modules, {"whisper": fake, "sounddevice": sd}), \
                mock.patch.object(config, "AUDIO_CHUNK_SECONDS", 2.0):
            stream = stt_service.transcribe_microphone("base", language="th")
            first, second = next(stream), next(stream)
        self.assertEqual(first[0], "hello")
        self.assertEqual(second[0], "again")
        self.assertEqual(recorded[0], (32000, 16000, 1, "float32"))
        self.assertEqual(len(first[1]), 32000 * 2)
        self.assertEqual(fake.transcribe_calls[0][1]["language"], "th")

    def test_missing_sounddevice_exits_with_an_install_hint(self):
        with mock.patch.dict(sys.modules, {"sounddevice": None}):
            with self.assertRaises(SystemExit) as ctx:
                next(stt_service.transcribe_microphone("base"))
        self.assertIn("sounddevice", str(ctx.exception))


class TextSourceTests(unittest.TestCase):
    def test_stdin_yields_one_stripped_non_blank_line_at_a_time(self):
        with mock.patch.object(sys, "stdin", io.StringIO("  hello  \n\n   \nworld\n")):
            self.assertEqual(list(stt_service.stdin_transcripts()), ["hello", "world"])

    def test_demo_cycles_through_the_builtin_sentences_forever(self):
        with mock.patch.object(stt_service.time, "sleep") as sleep:
            stream = stt_service.demo_transcripts(3.0)
            got = [next(stream) for _ in range(len(stt_service.DEMO_SENTENCES) + 2)]
        self.assertEqual(got[:4], stt_service.DEMO_SENTENCES)
        self.assertEqual(got[4:], stt_service.DEMO_SENTENCES[:2])
        sleep.assert_any_call(2.0)   # initial pause so listeners can register
        sleep.assert_any_call(3.0)


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv, stdin=None, transcripts=None):
        producer = mock.Mock()
        patches = [mock.patch.object(sys, "argv", ["stt_service.py", *argv]),
                   mock.patch.object(stt_service, "TranscriptProducer", return_value=producer)]
        if stdin is not None:
            patches.append(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
        if transcripts is not None:
            patches += [mock.patch.object(stt_service, "transcribe_file", return_value=iter(transcripts)),
                        mock.patch.object(stt_service, "demo_transcripts", return_value=iter(transcripts))]
        for p in patches:
            p.start()
        try:
            with mock.patch("builtins.print"):
                stt_service.main()
        finally:
            for p in patches:
                p.stop()
        return producer

    def test_stdin_lines_are_sent_as_numbered_chunks(self):
        producer = self.run_main("--stdin", stdin="hello\nworld\n")
        calls = producer.send.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["hello", "world"])
        self.assertTrue(calls[0].args[1].endswith("-1"))
        self.assertTrue(calls[1].args[1].endswith("-2"))
        self.assertEqual(calls[0].kwargs["source_lang"], "auto")
        producer.close.assert_called_once()

    def test_an_explicit_source_language_is_passed_through(self):
        producer = self.run_main("--stdin", "--source-lang", "th", stdin="hello\n")
        self.assertEqual(producer.send.call_args.kwargs["source_lang"], "th")

    def test_a_language_detected_per_chunk_replaces_auto_but_never_an_explicit_choice(self):
        item = ("hello", b"\x00\x01", "vi")
        auto = self.run_main("--input-file", __file__, transcripts=[item])
        self.assertEqual(auto.send.call_args.kwargs["source_lang"], "vi")
        explicit = self.run_main("--input-file", __file__, "--source-lang", "th", transcripts=[item])
        self.assertEqual(explicit.send.call_args.kwargs["source_lang"], "th")

    def test_an_unsupported_detected_language_stays_auto(self):
        producer = self.run_main("--input-file", __file__, transcripts=[("hello", b"\x00", "zz")])
        self.assertEqual(producer.send.call_args.kwargs["source_lang"], "auto")

    def test_audio_is_attached_only_with_send_audio(self):
        item = ("hello", b"\x00\x01", "en")
        self.assertIsNone(self.run_main("--input-file", __file__, transcripts=[item]).send.call_args.kwargs["audio_pcm"])
        attached = self.run_main("--input-file", __file__, "--send-audio", transcripts=[item])
        self.assertEqual(attached.send.call_args.kwargs["audio_pcm"], b"\x00\x01")

    def test_two_element_items_from_the_microphone_have_no_detected_language(self):
        producer = self.run_main("--input-file", __file__, transcripts=[("hello", b"\x00")])
        self.assertEqual(producer.send.call_args.kwargs["source_lang"], "auto")

    def test_demo_mode_sends_the_demo_stream(self):
        producer = self.run_main("--demo", transcripts=["demo one", "demo two"])
        self.assertEqual([c.args[0] for c in producer.send.call_args_list], ["demo one", "demo two"])

    def test_a_missing_input_file_is_a_usage_error(self):
        with mock.patch.object(sys, "argv", ["stt_service.py", "--input-file", "no-such-file.wav"]):
            with self.assertRaises(SystemExit):
                stt_service.main()

    def test_input_sources_are_mutually_exclusive(self):
        with mock.patch.object(sys, "argv", ["stt_service.py", "--stdin", "--demo"]):
            with self.assertRaises(SystemExit):
                stt_service.main()

    def test_ctrl_c_closes_the_producer(self):
        producer = mock.Mock()
        producer.send.side_effect = KeyboardInterrupt
        with mock.patch.object(sys, "argv", ["stt_service.py", "--stdin"]), \
                mock.patch.object(sys, "stdin", io.StringIO("hello\n")), \
                mock.patch.object(stt_service, "TranscriptProducer", return_value=producer), \
                mock.patch("builtins.print"):
            stt_service.main()
        producer.close.assert_called_once()


@unittest.skipUnless(np is not None, "numpy is installed only in the full image")
class WindowGuardTests(unittest.TestCase):
    """Silence and slivers of audio must not reach a speech model that would invent words."""

    def setUp(self):
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)

    def run_clip(self, samples, language="my", **env):
        class Clip(FakeWhisper):
            def load_audio(self, path):
                return samples
        calls, gaps = [], []
        with mock.patch.dict(sys.modules, {"whisper": Clip([], seconds=1)}), mock.patch.dict("os.environ", env),                 mock.patch.object(stt_service, "_transcribe_window_via_gemini",
                                  side_effect=lambda *a: calls.append(a) or "text"):
            out = list(stt_service.transcribe_file("x.wav", "base", language, lambda: gaps.append(1)))
        return out, calls, gaps

    def tone(self, seconds, level=0.05):
        return np.full(int(seconds * 16000), level, dtype=np.float32)

    def test_pure_silence_is_skipped_and_is_not_a_failure(self):
        out, calls, gaps = self.run_clip(np.zeros(16000 * 3, dtype=np.float32))
        self.assertEqual((out, calls, gaps), ([], [], []))

    def test_a_sliver_shorter_than_the_minimum_is_skipped(self):
        out, calls, gaps = self.run_clip(self.tone(0.4))
        self.assertEqual((out, calls, gaps), ([], [], []))

    def test_a_normal_clip_is_transcribed(self):
        out, calls, _ = self.run_clip(self.tone(2.0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(out[0][0], "text")

    def test_the_minimum_length_is_configurable(self):
        out, calls, _ = self.run_clip(self.tone(0.4), POLYGLOT_MIN_WINDOW_SECONDS="0.2")
        self.assertEqual(len(calls), 1)

    def test_a_quiet_but_real_voice_is_still_sent(self):
        out, calls, _ = self.run_clip(self.tone(2.0, level=0.01))
        self.assertEqual(len(calls), 1)

    def test_unmeasurable_audio_is_sent_rather_than_dropped(self):
        self.assertFalse(stt_service._window_is_unusable(object(), 16000))
        self.assertFalse(stt_service._window_is_unusable(list(range(100)), 1))

    def test_languages_whisper_can_read_are_not_filtered_by_this_guard(self):
        fake = FakeWhisper([{"language": "en", "segments": [seg("hello")]}], seconds=1)
        fake.load_audio = lambda path: np.zeros(16000, dtype=np.float32)
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            out = list(stt_service.transcribe_file("x.wav", "base", "en"))
        self.assertEqual(out[0][0], "hello")


class LazyModelTests(unittest.TestCase):
    def setUp(self):
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)

    def test_the_model_is_not_loaded_when_it_is_not_needed(self):
        fake = FakeWhisper([])
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            module, model = stt_service._load_whisper("base", need_model=False)
        self.assertIs(module, fake)
        self.assertIsNone(model)
        self.assertEqual(fake.loaded, [])

    def test_a_later_request_for_the_model_still_loads_it(self):
        fake = FakeWhisper([])
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            stt_service._load_whisper("base", need_model=False)
            _, model = stt_service._load_whisper("base")
        self.assertIs(model, fake.model)
        self.assertEqual(fake.loaded, ["base"])

    def test_a_cached_model_is_returned_even_when_not_asked_for(self):
        fake = FakeWhisper([])
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            stt_service._load_whisper("base")
            _, model = stt_service._load_whisper("base", need_model=False)
        self.assertIs(model, fake.model)

    def test_a_burmese_file_never_loads_the_model(self):
        class Clip(FakeWhisper):
            def load_audio(self, path):
                return np.full(32000, 0.05, dtype=np.float32) if np is not None else []
        fake = Clip([])
        with mock.patch.dict(sys.modules, {"whisper": fake}),                 mock.patch.object(stt_service, "_transcribe_window_via_gemini", return_value="ok"):
            if np is None:
                self.skipTest("numpy is installed only in the full image")
            list(stt_service.transcribe_file("x.wav", "base", "my"))
        self.assertEqual(fake.loaded, [])

    def test_auto_detect_still_needs_the_model(self):
        fake = FakeWhisper([], seconds=2)
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            if np is None:
                self.skipTest("numpy is installed only in the full image")
            fake.results = [{"language": "en", "segments": [seg("hi")]}]
            list(stt_service.transcribe_file("x.wav", "base", None))
        self.assertEqual(fake.loaded, ["base"])

    def test_the_model_less_path_does_not_wait_for_a_model_load_in_progress(self):
        fake = FakeWhisper([])
        started, release = threading.Event(), threading.Event()

        def slow_load(name):
            started.set()
            release.wait(5)
            return fake.model
        fake.load_model = slow_load
        with mock.patch.dict(sys.modules, {"whisper": fake}):
            loader = threading.Thread(target=stt_service._load_whisper, args=("base",), daemon=True)
            loader.start()
            self.assertTrue(started.wait(2))
            began = time.monotonic()
            stt_service._load_whisper("base", need_model=False)
            self.assertLess(time.monotonic() - began, 1.0)
            release.set()
            loader.join(3)


class PromptTests(unittest.TestCase):
    def test_the_transcription_prompt_forbids_inventing_words(self):
        try:
            import google.genai as genai
        except ImportError:
            self.skipTest("google-genai is installed only in the full image")
        captured = {}

        class Client:
            def __init__(self, **kwargs):
                self.models = self

            def generate_content(self, model, contents):
                captured["prompt"] = contents[0].parts[0].text
                return mock.Mock(text="")
        with mock.patch.object(genai, "Client", Client), mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}),                 mock.patch.object(stt_service, "_pcm_window_to_wav_bytes", return_value=b"wav"):
            stt_service._transcribe_window_via_gemini([0.1] * 16000, 16000, "my")
        self.assertIn("never guess or invent", captured["prompt"])
        self.assertIn("Burmese", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
