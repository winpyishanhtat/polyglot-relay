import pathlib
import unittest
from unittest import mock

from stt_service import (
    _GEMINI_STT_LANGUAGES,
    _detect_text_language,
    _transcribe_window_via_gemini,
    _transcribe_window_via_gemini_auto,
    transcribe_file,
)


class TextLanguageDetectionTests(unittest.TestCase):
    def test_returns_none_without_crashing_when_py3langid_unavailable(self):
        # The lightweight mock/translate images (and this test image) never
        # install py3langid - the lazy import must degrade gracefully to
        # "no signal available" rather than raising, matching how
        # _load_whisper's own lazy `import whisper` behaves.
        with mock.patch.dict("sys.modules", {"py3langid": None}):
            result = _detect_text_language("some text")
        self.assertIsNone(result)


class GeminiSttFallbackTests(unittest.TestCase):
    """Whisper cannot reliably transcribe my/km/lo at any practical model
    size (confirmed by direct testing); these languages instead route
    through Gemini's own audio understanding. Covers the no-key guard here
    - the actual API call needs a real key and audio, exercised manually."""

    def test_covers_exactly_the_three_confirmed_weak_languages(self):
        self.assertEqual(_GEMINI_STT_LANGUAGES, frozenset({"my", "km", "lo"}))

    def test_returns_none_without_crashing_when_no_api_key_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = _transcribe_window_via_gemini(window=object(), sample_rate=16000, language="my")
        self.assertIsNone(result)

    def test_prefers_per_language_key_over_shared_key(self):
        # A GEMINI_API_KEY_MY override should be tried before the shared
        # GEMINI_API_KEY - same precedence as translator_worker.py's
        # _get_client, so my/km/lo can each get their own quota pool.
        fake_genai = mock.MagicMock()
        fake_genai.Client.return_value.models.generate_content.return_value = mock.MagicMock(text="ok")
        fake_types = mock.MagicMock()
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY_MY": "per-lang-key", "GEMINI_API_KEY": "shared-key"}):
            with mock.patch.dict("sys.modules", {"google": mock.MagicMock(genai=fake_genai), "google.genai": fake_genai, "google.genai.types": fake_types}):
                with mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"):
                    _transcribe_window_via_gemini(window=object(), sample_rate=16000, language="my")
        fake_genai.Client.assert_called_once_with(api_key="per-lang-key", http_options={"timeout": 60000})


def _gemini_modules(generate_content):
    """sys.modules patch faking the google SDK around a generate_content stub."""
    fake_genai = mock.MagicMock()
    fake_genai.Client.return_value.models.generate_content = generate_content
    return {"google": mock.MagicMock(genai=fake_genai), "google.genai": fake_genai,
            "google.genai.types": mock.MagicMock()}


class GeminiTranscriptionRetryTests(unittest.TestCase):
    """A live Burmese session hit 503 (overloaded) and 429 (quota) from
    Gemini; transient failures are retried briefly, and everything else gives
    up promptly - returning None ("could not transcribe"), never Whisper."""

    def _call(self, generate_content, **env):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", **env}, clear=True),              mock.patch.dict("sys.modules", _gemini_modules(generate_content)),              mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"),              mock.patch("stt_service.time.sleep") as sleep:
            return _transcribe_window_via_gemini(object(), 16000, "my"), sleep

    def test_transient_error_is_retried_then_succeeds(self):
        call = mock.Mock(side_effect=[Exception("503 UNAVAILABLE. high demand"), mock.Mock(text="  မင်္ဂလာပါ  ")])
        result, sleep = self._call(call)
        self.assertEqual(result, "မင်္ဂလာပါ")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once()

    def test_gives_up_after_the_configured_retries(self):
        call = mock.Mock(side_effect=Exception("503 UNAVAILABLE"))
        result, sleep = self._call(call)
        self.assertIsNone(result)
        self.assertEqual(call.call_count, 3)  # first attempt + GEMINI_STT_MAX_RETRIES (2)
        self.assertEqual(sleep.call_count, 2)

    def test_retry_count_is_configurable(self):
        call = mock.Mock(side_effect=Exception("503 UNAVAILABLE"))
        result, _ = self._call(call, GEMINI_STT_MAX_RETRIES="0")
        self.assertIsNone(result)
        self.assertEqual(call.call_count, 1)

    def test_a_non_transient_error_is_not_retried(self):
        call = mock.Mock(side_effect=Exception("400 INVALID_ARGUMENT: API key not valid"))
        result, sleep = self._call(call)
        self.assertIsNone(result)
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_a_retry_hint_longer_than_a_live_slice_can_wait_is_not_retried(self):
        call = mock.Mock(side_effect=Exception("429 RESOURCE_EXHAUSTED {'retryDelay': '30s'}"))
        result, sleep = self._call(call)
        self.assertIsNone(result)
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_an_empty_transcript_means_silence_not_failure(self):
        result, _ = self._call(mock.Mock(return_value=mock.Mock(text="")))
        self.assertEqual(result, "")


class GapInsteadOfWhisperTests(unittest.TestCase):
    """Whisper cannot recognise my/km/lo, so when Gemini fails the window must
    be skipped (and reported), not transcribed into nonsense by Whisper."""

    def _run(self, language, gemini_results, whisper_result=None):
        whisper = mock.Mock()
        whisper.load_audio.return_value = list(range(35))  # three windows at 15s
        whisper.audio.SAMPLE_RATE = 1
        model = mock.Mock()
        model.transcribe.return_value = whisper_result or {"language": language, "segments": [{"text": "nonsense"}]}
        gaps = []
        with mock.patch("stt_service._load_whisper", return_value=(whisper, model)),              mock.patch("stt_service._pcm16", return_value=b"pcm"),              mock.patch("stt_service._transcribe_window_via_gemini", side_effect=gemini_results) as gemini,              mock.patch.dict("os.environ", {"POLYGLOT_FILE_CHUNK_SECONDS": "15"}):
            out = list(transcribe_file(pathlib.Path("x.wav"), "base", language, on_unrecognized=lambda: gaps.append(1)))
        return out, gaps, model, gemini

    def test_failed_burmese_windows_leave_gaps_and_never_reach_whisper(self):
        out, gaps, model, _ = self._run("my", [None, "ok", ""])
        self.assertEqual(out, [("ok", b"pcm", "my")])
        self.assertEqual(len(gaps), 1)            # only the failed window; "" is silence
        model.transcribe.assert_not_called()

    def test_auto_detected_burmese_does_not_keep_whispers_text_when_gemini_fails(self):
        out, gaps, model, _ = self._run(None, [None, None, None], {"language": "my", "segments": [{"text": "Fanameva"}]})
        self.assertEqual(out, [])
        self.assertEqual(len(gaps), 3)

    def test_languages_whisper_can_do_are_unaffected(self):
        out, gaps, model, gemini = self._run("th", [])
        self.assertEqual([c[0] for c in out], ["nonsense"] * 3)  # Whisper's text, by design, for th
        self.assertEqual(gaps, [])
        gemini.assert_not_called()


class GeminiTranscriptionModelChoiceTests(unittest.TestCase):
    """Speech transcription can use a different model from translation: a
    Flash-Lite model that is fine for text took 15-20s per 4s live slice."""

    def _model_used(self, env):
        generate = mock.Mock(return_value=mock.Mock(text="ok"))
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k", **env}, clear=True),              mock.patch.dict("sys.modules", _gemini_modules(generate)),              mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"):
            _transcribe_window_via_gemini(object(), 16000, "my")
        return generate.call_args.kwargs["model"]

    def test_dedicated_stt_model_wins(self):
        self.assertEqual(self._model_used({"GEMINI_STT_MODEL": "fast-stt", "GEMINI_TEXT_MODEL": "slow-text"}), "fast-stt")

    def test_falls_back_to_the_text_model_then_to_flash_latest(self):
        self.assertEqual(self._model_used({"GEMINI_TEXT_MODEL": "slow-text"}), "slow-text")
        self.assertEqual(self._model_used({}), "gemini-flash-latest")


class GeminiAutoDetectRecoveryTests(unittest.TestCase):
    """Covers the "Auto-detect" recovery path: triggered when Whisper's own
    detected language falls outside the 9 supported codes entirely - the
    exact signature both confirmed real Burmese failures showed (Whisper
    mis-detected the language, not just transcribed it poorly)."""

    def test_returns_none_without_crashing_when_no_api_key_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = _transcribe_window_via_gemini_auto(window=object(), sample_rate=16000)
        self.assertIsNone(result)

    def test_parses_language_and_text_from_a_well_formed_reply(self):
        fake_genai = mock.MagicMock()
        fake_genai.Client.return_value.models.generate_content.return_value = mock.MagicMock(
            text="LANGUAGE: my\nTEXT: some burmese text"
        )
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "shared-key"}):
            with mock.patch.dict("sys.modules", {"google": mock.MagicMock(genai=fake_genai), "google.genai": fake_genai, "google.genai.types": mock.MagicMock()}):
                with mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"):
                    result = _transcribe_window_via_gemini_auto(window=object(), sample_rate=16000)
        self.assertEqual(result, ("some burmese text", "my"))

    def test_rejects_a_language_outside_the_supported_set(self):
        # A malformed or out-of-set reply must not be trusted - fall back to
        # keeping Whisper's own (wrong) result rather than propagating junk.
        fake_genai = mock.MagicMock()
        fake_genai.Client.return_value.models.generate_content.return_value = mock.MagicMock(
            text="LANGUAGE: zh\nTEXT: some text"
        )
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "shared-key"}):
            with mock.patch.dict("sys.modules", {"google": mock.MagicMock(genai=fake_genai), "google.genai": fake_genai, "google.genai.types": mock.MagicMock()}):
                with mock.patch("stt_service._pcm_window_to_wav_bytes", return_value=b"wav"):
                    result = _transcribe_window_via_gemini_auto(window=object(), sample_rate=16000)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
