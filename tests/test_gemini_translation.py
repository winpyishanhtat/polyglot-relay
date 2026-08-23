import base64
import json
import unittest

from stt_service import TranscriptProducer
from translator_worker import Translator


class CaptureSocket:
    def __init__(self):
        self.payload = b""

    def sendall(self, payload):
        self.payload += payload

    def close(self):
        pass


class GeminiPipelineTests(unittest.TestCase):
    def test_mock_translator_keeps_offline_mode(self):
        text, backend = Translator("ja", mock=True).translate("hello")
        self.assertEqual(text, "[ja] hello")
        self.assertEqual(backend, "mock")

    def test_live_translation_rejects_wrong_sample_rate_before_api_call(self):
        encoded = base64.b64encode(b"\x00\x00" * 10).decode("ascii")
        with self.assertRaisesRegex(ValueError, "16 kHz"):
            Translator("my").translate("speech", encoded, audio_rate=8000)

    def test_producer_attaches_pcm_audio_to_transcript_chunk(self):
        producer = TranscriptProducer()
        producer.sock = CaptureSocket()
        producer.send("hello", "chunk-1", audio_pcm=b"\x01\x02", audio_rate=16000)
        message = json.loads(producer.sock.payload)
        self.assertEqual(message["audio_pcm_b64"], "AQI=")
        self.assertEqual(message["audio_rate"], 16000)


if __name__ == "__main__":
    unittest.main()
