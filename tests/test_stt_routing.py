"""Which recogniser transcribes which language: Gemini first for every known language, Whisper only as a
fallback for languages it can read. (Live Vietnamese through Whisper dropped 26 of 29 slices and returned
nonsense; on real speech Gemini made 2-7x fewer character errors in every language.)"""

import pathlib
import threading
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
from unittest import mock

import eval.fleurs_eval as fleurs_eval
import stt_service
from common import config
from stt_service import gemini_first, transcribe_file


class GeminiFirstTests(unittest.TestCase):
    def setting(self, value):
        env = {} if value is None else {"POLYGLOT_GEMINI_STT_LANGUAGES": value}
        return mock.patch.dict("os.environ", env, clear=False)

    def clear(self):
        patcher = mock.patch.dict("os.environ")
        patcher.start()
        self.addCleanup(patcher.stop)
        __import__("os").environ.pop("POLYGLOT_GEMINI_STT_LANGUAGES", None)

    def test_every_known_language_goes_to_gemini_by_default(self):
        self.clear()
        for code in config.TARGET_LANGUAGES:
            self.assertTrue(gemini_first(code), code)

    def test_an_empty_setting_is_the_default_as_compose_passes_unset_values(self):
        with self.setting(""):
            self.assertTrue(gemini_first("vi"))

    def test_an_unknown_spoken_language_is_not_sent_to_gemini(self):
        self.clear()
        self.assertFalse(gemini_first(None))
        self.assertFalse(gemini_first("zz"))

    def test_none_sends_languages_whisper_can_read_back_to_whisper(self):
        with self.setting("none"):
            for code in ("en", "id", "ms", "th", "vi", "tl"):
                self.assertFalse(gemini_first(code), code)

    def test_burmese_khmer_and_lao_are_always_gemini_because_whisper_cannot_read_them(self):
        for value in ("none", "en", "vi,th", "", "all"):
            with self.setting(value):
                for code in ("my", "km", "lo"):
                    self.assertTrue(gemini_first(code), (value, code))

    def test_a_list_selects_exactly_those_languages_and_tolerates_spaces_and_case(self):
        with self.setting(" VI , Th "):
            self.assertTrue(gemini_first("vi"))
            self.assertTrue(gemini_first("th"))
            self.assertFalse(gemini_first("en"))
            self.assertFalse(gemini_first("id"))

    def test_all_is_case_insensitive(self):
        with self.setting("ALL"):
            self.assertTrue(gemini_first("tl"))


class FakeWhisper:
    SAMPLE_RATE = 16000

    def __init__(self, text="whisper text", windows=2):
        self.text, self.windows = text, windows
        self.audio = type("A", (), {"SAMPLE_RATE": 16000})()
        self.loaded, self.transcribed = [], 0
        self.lock_held_during_transcribe = []
        self.model = type("M", (), {"transcribe": self._transcribe})()

    def load_audio(self, path):
        return [0.1] * (self.windows * 15 * 16000)

    def load_model(self, name):
        self.loaded.append(name)
        return self.model

    def _transcribe(self, window, **kwargs):
        self.transcribed += 1
        self.lock_held_during_transcribe.append(stt_service._WHISPER_FALLBACK_LOCK.locked())
        # Auto-detect asks for the language back: answer with a supported one so no Gemini recovery is attempted.
        return {"language": kwargs.get("language") or "en", "segments": [{"text": self.text, "no_speech_prob": 0, "avg_logprob": 0}]}


class RoutingInTranscribeFileTests(unittest.TestCase):
    def setUp(self):
        for name in ("_transcribe_window_via_gemini_auto",):
            patcher = mock.patch.object(stt_service, name, side_effect=AssertionError("a test reached the real Gemini API"))
            patcher.start()
            self.addCleanup(patcher.stop)
        stt_service._WHISPER_CACHE.clear()
        self.addCleanup(stt_service._WHISPER_CACHE.clear)
        # Synthetic signals, not speech: the voice check has its own tests.
        voice = mock.patch.object(stt_service.voice_check, "not_a_voice", return_value=False)
        voice.start()
        self.addCleanup(voice.stop)
        patcher = mock.patch.dict("os.environ", {"POLYGLOT_FILE_CHUNK_SECONDS": "15"})
        patcher.start()
        self.addCleanup(patcher.stop)
        __import__("os").environ.pop("POLYGLOT_GEMINI_STT_LANGUAGES", None)

    def run_file(self, language, gemini, whisper=None, gaps=None):
        whisper = whisper or FakeWhisper()
        with mock.patch("stt_service._load_whisper", side_effect=lambda name, need_model=True: (whisper, whisper.model if need_model else None)) as load, \
                mock.patch("stt_service._window_is_unusable", return_value=False), \
                mock.patch("stt_service._pcm16", return_value=b"pcm"), \
                mock.patch("stt_service._transcribe_window_via_gemini", side_effect=gemini) as call:
            out = list(transcribe_file(pathlib.Path("x.wav"), "base", language,
                                       on_unrecognized=(lambda: gaps.append(1)) if gaps is not None else None))
        return out, whisper, load, call

    def test_vietnamese_is_transcribed_by_gemini_and_whisper_is_never_asked(self):
        out, whisper, load, call = self.run_file("vi", ["xin chao", "tam biet"])
        self.assertEqual([c[0] for c in out], ["xin chao", "tam biet"])
        self.assertEqual(out[0][2], "vi")
        self.assertEqual(whisper.transcribed, 0)
        self.assertEqual(load.call_args.kwargs.get("need_model", True), False)    # the model is not even loaded
        self.assertEqual(call.call_args.args[2], "vi")

    def test_when_gemini_is_unavailable_a_whisper_language_falls_back_to_whisper(self):
        gaps = []
        out, whisper, _, _ = self.run_file("vi", [None, "tam biet"], gaps=gaps)
        self.assertEqual([c[0] for c in out], ["whisper text", "tam biet"])
        self.assertEqual(gaps, [])                      # a fallback caption is not a gap
        self.assertEqual(whisper.transcribed, 1)

    def test_the_model_is_loaded_lazily_only_when_the_fallback_is_needed(self):
        out, whisper, load, _ = self.run_file("th", ["a", "b"])
        self.assertEqual(whisper.transcribed, 0)
        out, whisper, load, _ = self.run_file("th", [None, "b"])
        loads = [call.kwargs.get("need_model", True) for call in load.call_args_list]
        self.assertEqual(loads, [False, True])           # model-less at the start, then the model for the fallback

    def test_burmese_still_leaves_a_gap_and_never_uses_whisper(self):
        gaps = []
        out, whisper, _, _ = self.run_file("my", [None, "ok"], gaps=gaps)
        self.assertEqual([c[0] for c in out], ["ok"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(whisper.transcribed, 0)

    def test_silence_from_gemini_is_skipped_without_whisper(self):
        out, whisper, _, _ = self.run_file("vi", ["", "words"])
        self.assertEqual([c[0] for c in out], ["words"])
        self.assertEqual(whisper.transcribed, 0)

    def test_the_fallback_runs_one_whisper_inference_at_a_time(self):
        out, whisper, _, _ = self.run_file("vi", [None, None])
        self.assertEqual(whisper.lock_held_during_transcribe, [True, True])

    def test_switching_gemini_off_gives_the_original_whisper_behaviour(self):
        with mock.patch.dict("os.environ", {"POLYGLOT_GEMINI_STT_LANGUAGES": "none"}):
            out, whisper, load, call = self.run_file("vi", [])
        self.assertEqual([c[0] for c in out], ["whisper text", "whisper text"])
        call.assert_not_called()
        self.assertTrue(load.call_args.kwargs.get("need_model", True))

    def test_auto_detect_is_unchanged_and_still_uses_whisper_first(self):
        out, whisper, load, call = self.run_file(None, [])
        self.assertEqual(whisper.transcribed, 2)
        call.assert_not_called()


class EvaluationModesTests(unittest.TestCase):
    def test_whisper_mode_switches_gemini_off_during_the_run_and_restores_the_setting(self):
        seen = []

        def fake_transcribe_file(path, model, language=None, on_unrecognized=None):
            seen.append(gemini_first(language))
            yield "t", b"", language
        with mock.patch("stt_service.transcribe_file", fake_transcribe_file), mock.patch.dict("os.environ", {"POLYGLOT_GEMINI_STT_LANGUAGES": "vi"}):
            fleurs_eval.default_transcriber("base", "whisper")("a.wav", "vi")
            self.assertEqual(__import__("os").environ["POLYGLOT_GEMINI_STT_LANGUAGES"], "vi")      # restored
            fleurs_eval.default_transcriber("base", "default")("a.wav", "vi")
        self.assertEqual(seen, [False, True])

    def test_the_setting_is_removed_again_when_it_was_not_set_before(self):
        def fake_transcribe_file(path, model, language=None, on_unrecognized=None):
            yield "t", b"", language
        env = __import__("os").environ
        with mock.patch.dict("os.environ"):
            env.pop("POLYGLOT_GEMINI_STT_LANGUAGES", None)
            with mock.patch("stt_service.transcribe_file", fake_transcribe_file):
                fleurs_eval.default_transcriber("base", "whisper")("a.wav", "th")
            self.assertNotIn("POLYGLOT_GEMINI_STT_LANGUAGES", env)


if __name__ == "__main__":
    unittest.main()
