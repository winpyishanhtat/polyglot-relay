"""Read-aloud extras: clips kept on disk, a voice per language, and the daily speech allowance."""

import datetime as dt
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import tts
import web_ui
from test_web_features import FakeSdk, audio_response


class Env(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        patcher = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_MODEL": "", "GEMINI_TTS_VOICE": "",
                                               "POLYGLOT_TTS_DAILY_LIMIT": "", "POLYGLOT_TTS_CACHE_DIR": "", "GEMINI_TTS_VOICE_TH": ""})
        patcher.start()
        self.addCleanup(patcher.stop)


class DiskCacheTests(Env):
    def test_a_clip_made_once_survives_a_restart(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00" * 8)) as sdk:
            first = tts.SpeechSynthesizer(cache_dir=self.dir).synthesize("hello", "en")
            again = tts.SpeechSynthesizer(cache_dir=self.dir).synthesize("hello", "en")   # a new process, same disk
        self.assertEqual(first, again)
        self.assertEqual(len(sdk.models.calls), 1)
        self.assertEqual(len([n for n in os.listdir(self.dir) if n.endswith(".wav")]), 1)

    def test_a_different_voice_or_language_is_a_different_clip(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            synth = tts.SpeechSynthesizer(cache_dir=self.dir)
            synth.synthesize("hello", "en")
            synth.synthesize("hello", "en", voice="Puck")
            synth.synthesize("hello", "th")
        self.assertEqual(len(sdk.models.calls), 3)

    def test_a_damaged_clip_on_disk_is_made_again(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00" * 4)) as sdk:
            synth = tts.SpeechSynthesizer(cache_dir=self.dir)
            synth.synthesize("hello", "en")
            for name in os.listdir(self.dir):
                if name.endswith(".wav"):
                    with open(os.path.join(self.dir, name), "wb") as handle:
                        handle.write(b"garbage")
            tts.SpeechSynthesizer(cache_dir=self.dir).synthesize("hello", "en")
        self.assertEqual(len(sdk.models.calls), 2)

    def test_the_disk_cache_is_pruned_to_its_size_oldest_first(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_TTS_CACHE_MB": "1"}), \
                FakeSdk(lambda m, c, n: audio_response(b"\x01\x00" * 60000)):          # about 120 KB a clip
            synth = tts.SpeechSynthesizer(cache_dir=self.dir)
            for n in range(20):
                synth.synthesize(f"phrase {n}", "en")
        total = sum(os.path.getsize(os.path.join(self.dir, n)) for n in os.listdir(self.dir) if n.endswith(".wav"))
        self.assertLessEqual(total, 1024 * 1024)

    def test_an_unusable_cache_directory_falls_back_to_memory(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").close()
        with self.assertLogs("polyglot.tts", level="WARNING"):
            synth = tts.SpeechSynthesizer(cache_dir=os.path.join(blocker, "sub"))
        self.assertEqual(synth.cache_dir, "")
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")):
            self.assertEqual(synth.synthesize("hello", "en")[:4], b"RIFF")

    def test_the_cache_directory_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_TTS_CACHE_DIR": self.dir}):
            self.assertEqual(tts.SpeechSynthesizer().cache_dir, self.dir)


class VoiceTests(Env):
    def voice_used(self, **kwargs):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            tts.SpeechSynthesizer(cache_dir="").synthesize("hello", kwargs.pop("lang", "th"), **kwargs)
        return sdk.models.calls[0]["config"].speech_config.voice_config.prebuilt_voice_config.voice_name

    def test_the_default_voice(self):
        self.assertEqual(self.voice_used(), tts.DEFAULT_VOICE)

    def test_a_voice_can_be_set_per_language(self):
        with mock.patch.dict(os.environ, {"GEMINI_TTS_VOICE_TH": "Leda", "GEMINI_TTS_VOICE": "Puck"}):
            self.assertEqual(self.voice_used(lang="th"), "Leda")
            self.assertEqual(self.voice_used(lang="vi"), "Puck")

    def test_a_request_may_choose_a_voice_by_name_in_any_case(self):
        self.assertEqual(self.voice_used(voice="charon"), "Charon")

    def test_an_unknown_voice_is_refused_before_any_call(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk, self.assertRaises(tts.TtsError) as caught:
            tts.SpeechSynthesizer(cache_dir="").synthesize("hello", "th", voice="Robot")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(sdk.models.calls, [])


class QuotaTests(Env):
    def test_calls_are_counted_per_model_and_cache_hits_are_free(self):
        with FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")):
            synth = tts.SpeechSynthesizer(cache_dir="")
            synth.synthesize("a", "en")
            synth.synthesize("a", "en")
            synth.synthesize("b", "en")
        report = synth.quota_report()
        self.assertEqual(report["models"][0], {"model": tts.DEFAULT_MODEL, "used": 2, "left": None})
        self.assertIsNone(report["left"])                       # no limit configured
        self.assertFalse(report["low"])

    def test_with_a_limit_the_page_is_told_what_is_left_and_when_it_is_low(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_TTS_DAILY_LIMIT": "3"}), FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")):
            synth = tts.SpeechSynthesizer(cache_dir="")
            self.assertEqual(synth.quota_report()["left"], 6)       # 3 each on the two models
            for n in range(3):
                synth.synthesize(f"p{n}", "en")
            report = synth.quota_report()
        self.assertEqual(report["left"], 3)
        self.assertTrue(report["low"])

    def test_a_used_up_model_is_skipped_and_the_fallback_used(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_TTS_DAILY_LIMIT": "1"}), FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            synth = tts.SpeechSynthesizer(cache_dir="")
            synth.synthesize("one", "en")
            synth.synthesize("two", "en")
            with self.assertRaises(tts.TtsError) as caught:
                synth.synthesize("three", "en")
            self.assertTrue(synth.quota_report()["exhausted"])
        self.assertEqual([c["model"] for c in sdk.models.calls], [tts.DEFAULT_MODEL, tts.FALLBACK_MODEL])
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("daily", str(caught.exception))

    def test_googles_daily_quota_answer_stops_further_calls_today(self):
        def behaviour(model, config, n):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel")
        with FakeSdk(behaviour) as sdk:
            synth = tts.SpeechSynthesizer(cache_dir="")
            with self.assertRaises(tts.TtsError):
                synth.synthesize("one", "en")
            calls = len(sdk.models.calls)
            with self.assertRaises(tts.TtsError) as caught:
                synth.synthesize("two", "en")
        self.assertEqual(len(sdk.models.calls), calls)             # no pointless second round
        self.assertIn("daily", str(caught.exception))
        self.assertEqual(synth.quota_report()["left"], 0)

    def test_the_count_survives_a_restart_on_the_same_day_and_resets_the_next(self):
        path = os.path.join(self.dir, "quota.json")
        first = tts.QuotaTracker(path, limit=5)
        first.record("m")
        first.record("m")
        self.assertEqual(tts.QuotaTracker(path, limit=5).report(["m"])["left"], 3)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        saved["day"] = "2000-01-01"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(saved, handle)
        self.assertEqual(tts.QuotaTracker(path, limit=5).report(["m"])["left"], 5)

    def test_the_day_rolls_over_while_running(self):
        tracker = tts.QuotaTracker(None, limit=2)
        tracker.exhaust("m")
        self.assertFalse(tracker.allowed("m"))
        tracker._day = "2000-01-01"                                  # as if midnight Pacific had passed
        self.assertTrue(tracker.allowed("m"))

    def test_a_damaged_quota_file_starts_from_zero(self):
        path = os.path.join(self.dir, "quota.json")
        with open(path, "w") as handle:
            handle.write("{broken")
        self.assertEqual(tts.QuotaTracker(path, limit=4).report(["m"])["left"], 4)

    def test_the_quota_day_is_the_pacific_day(self):
        late_utc = dt.datetime(2026, 1, 2, 5, 0, tzinfo=dt.timezone.utc)      # 21:00 on 1 January in California
        self.assertEqual(tts.pacific_day(late_utc), "2026-01-01")


class EndpointTests(Env):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", "POLYGLOT_ACCESS_TOKEN": "", "POLYGLOT_VIEW_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = web_ui.DashboardRuntime()
        self.runtime.producer = mock.Mock()
        web_ui.DashboardHandler.runtime = self.runtime
        web_ui.DashboardHandler.limiter = web_ui.RequestRateLimiter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_ui.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runtime.stop)
        synth = tts.SpeechSynthesizer(cache_dir="")
        patcher = mock.patch.object(tts, "SYNTHESIZER", synth)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.synth = synth

    def test_the_info_lists_voices_and_the_allowance(self):
        with urllib.request.urlopen(self.base + "/api/tts/info", timeout=5) as response:
            info = json.load(response)
        self.assertIn("Kore", info["voices"])
        self.assertIn("left", info["quota"])
        self.assertEqual(info["warn_when_left"], tts.WARN_WHEN_LEFT)

    def test_a_chosen_voice_reaches_the_synthesizer_and_the_reply_says_what_is_left(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_TTS_DAILY_LIMIT": "4"}), FakeSdk(lambda m, c, n: audio_response(b"\x01\x00")) as sdk:
            request = urllib.request.Request(self.base + "/api/tts", method="POST", headers={"Content-Type": "application/json"},
                                             data=json.dumps({"text": "hi", "lang": "th", "voice": "Puck"}).encode())
            with urllib.request.urlopen(request, timeout=5) as response:
                left = response.headers["X-Speech-Quota-Left"]
        self.assertEqual(sdk.models.calls[0]["config"].speech_config.voice_config.prebuilt_voice_config.voice_name, "Puck")
        self.assertEqual(left, "7")

    def test_an_unknown_voice_is_a_400(self):
        request = urllib.request.Request(self.base + "/api/tts", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"text": "hi", "lang": "th", "voice": "Robot"}).encode())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
