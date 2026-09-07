import unittest
from unittest import mock

from stt_service import (
    _GEMINI_STT_LANGUAGES,
    _detect_text_language,
    _transcribe_window_via_gemini,
    _transcribe_window_via_gemini_auto,
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
