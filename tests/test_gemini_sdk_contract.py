import inspect
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

try:
    from google.genai import types
    from google.genai.live import AsyncSession
except ImportError:
    types = None
    AsyncSession = None


@unittest.skipUnless(types is not None, "google-genai is installed only in the full image")
class GeminiSdkContractTests(unittest.TestCase):
    def test_live_translation_config_and_audio_stream_end_are_supported(self):
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code="ja",
                echo_target_language=True,
            ),
        )
        self.assertEqual(config.translation_config.target_language_code, "ja")
        parameters = inspect.signature(AsyncSession.send_realtime_input).parameters
        self.assertIn("audio", parameters)
        self.assertIn("audio_stream_end", parameters)


if __name__ == "__main__":
    unittest.main()
