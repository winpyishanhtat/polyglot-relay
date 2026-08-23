import unittest

from stt_service import _detect_text_language


class TextLanguageDetectionTests(unittest.TestCase):
    def test_returns_none_without_crashing_when_py3langid_unavailable(self):
        # The lightweight mock/translate images (and this test image) never
        # install py3langid - the lazy import must degrade gracefully to
        # "no signal available" rather than raising, matching how
        # _load_whisper's own lazy `import whisper` behaves.
        result = _detect_text_language("some text")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
