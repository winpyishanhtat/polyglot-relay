"""voice_check.py: steady sounds with no voice in them are not transcribed; speech always is."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401

try:
    import numpy as np
except ImportError:          # the stdlib-only Docker test image
    np = None

import voice_check

RATE = 16000


def speech_like(seconds=3.0, seed=0):
    """Harmonic tones (a voice's pitch and formants) switched on and off at a syllable rate of about 4 Hz."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    voiced = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 25) if 140 * k < 4000)
    envelope = (np.sin(2 * np.pi * 4 * t) > 0).astype(float) * (0.6 + 0.4 * rng.random(t.size))
    return 0.2 * voiced / np.abs(voiced).max() * envelope + 0.002 * rng.standard_normal(t.size)


@unittest.skipUnless(np is not None, "numpy is not installed")
class VoiceCheckTests(unittest.TestCase):
    def test_speech_like_sound_is_kept(self):
        self.assertIsNone(voice_check.reason(speech_like(), RATE))

    def test_white_noise_is_rejected_as_hiss(self):
        noise = 0.02 * np.random.default_rng(1).standard_normal(3 * RATE)
        self.assertIn("flat", voice_check.reason(noise, RATE))

    def test_mains_hum_is_rejected(self):
        t = np.arange(3 * RATE) / RATE
        hum = 0.3 * (np.sin(2 * np.pi * 60 * t) + 0.5 * np.sin(2 * np.pi * 120 * t))
        self.assertIn("voice band", voice_check.reason(hum, RATE))

    def test_a_steady_tone_in_the_voice_band_is_not_rejected_as_hum(self):
        t = np.arange(3 * RATE) / RATE
        self.assertIsNone(voice_check.reason(0.2 * np.sin(2 * np.pi * 1000 * t), RATE))

    def test_too_short_or_unmeasurable_audio_is_left_to_the_transcriber(self):
        self.assertIsNone(voice_check.reason(np.zeros(500), RATE))
        self.assertIsNone(voice_check.reason("not audio", RATE))
        self.assertIsNone(voice_check.reason(np.zeros(3 * RATE), RATE))       # pure silence: the silence check's job

    def test_it_can_be_switched_off(self):
        noise = 0.02 * np.random.default_rng(1).standard_normal(3 * RATE)
        with mock.patch.dict(os.environ, {"POLYGLOT_VOICE_CHECK": "0"}):
            self.assertFalse(voice_check.not_a_voice(noise, RATE))
        self.assertTrue(voice_check.not_a_voice(noise, RATE))


class MeasurementTests(unittest.TestCase):
    def test_the_committed_measurement_rejected_no_real_recording(self):
        report = json.loads((Path(__file__).resolve().parents[1] / "results" / "voice_check.json").read_text(encoding="utf-8"))
        self.assertEqual(report["speech_recordings"], 1350)
        self.assertEqual(report["speech_rejected"], 0)
        self.assertLess(report["speech_windows_rejected"] / report["speech_windows_3s"], 0.01)
        hums = [row for row in report["noise"] if row["name"].startswith(("hum", "white", "pink"))]
        self.assertTrue(all(row["rejected"] for row in hums))


if __name__ == "__main__":
    unittest.main()
