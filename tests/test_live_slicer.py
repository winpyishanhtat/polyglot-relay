import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Feeds the real ui/live_slicer.js synthetic loudness readings (one per 50ms)
# and reports every cut it asks for.
DRIVER = r"""
const { LiveSlicer } = require(process.argv[1]);
let seed = 7;
const jitter = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff - 0.5; };
function run(segments, options) {
  const slicer = new LiveSlicer(options);
  const cuts = [];
  let now = 0;
  for (const [seconds, level, wobble] of segments) {
    for (let i = 0; i < Math.round(seconds * 20); i++) {
      now += 50;
      const d = slicer.push(Math.max(0, level + (wobble || 0) * jitter()), now);
      if (d.cut) cuts.push({ at: now / 1000, reason: d.reason, hadSpeech: d.hadSpeech });
    }
  }
  return cuts;
}
const SPEECH = 0.2, QUIET = 0.002;
console.log(JSON.stringify({
  phrase_then_pause: run([[3, SPEECH, 0.1], [1, QUIET]]),
  continuous_speech: run([[20, SPEECH, 0.1]]),
  pause_before_min_length: run([[1, SPEECH, 0.1], [0.5, QUIET], [2, SPEECH, 0.1], [1, QUIET]]),
  silence_only: run([[20, QUIET]]),
  speaks_immediately: run([[2.5, 0.15, 0.08], [1, QUIET]]),
  noisy_room: run([[3, 0.02, 0.01], [3, SPEECH, 0.1], [1, 0.02, 0.01]]),
  quiet_speaker_with_natural_dips: run([[0.8, 0.02, 0.006], [0.2, 0.002], [0.8, 0.02, 0.006], [0.2, 0.002], [0.8, 0.02, 0.006], [0.2, 0.002], [1, 0.001]]),
  quiet_speaker_from_the_first_moment: run([[3, 0.02, 0.006], [6, 0.001]]),
  loud_continuous_talker_in_loud_room: run([[8.5, 0.2, 0.02]]),
  custom_limits: run([[10, SPEECH, 0.1]], { maxMs: 4000 }),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LiveSlicerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run(["node", "-e", DRIVER, os.path.join(ROOT, "ui", "live_slicer.js")],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr)
        cls.out = json.loads(done.stdout)

    def test_a_phrase_is_cut_at_the_pause_after_it(self):
        (cut,) = self.out["phrase_then_pause"]
        self.assertEqual(cut["reason"], "pause")
        self.assertTrue(cut["hadSpeech"])
        self.assertAlmostEqual(cut["at"], 3.4, delta=0.2)  # 3.0s of speech + the 0.35s pause

    def test_continuous_speech_is_force_cut_at_the_maximum_and_never_dropped(self):
        cuts = self.out["continuous_speech"]
        self.assertEqual([c["reason"] for c in cuts], ["max", "max"])
        self.assertEqual([round(c["at"]) for c in cuts], [8, 16])
        self.assertTrue(all(c["hadSpeech"] for c in cuts))

    def test_a_pause_before_the_minimum_length_does_not_cut(self):
        (cut,) = self.out["pause_before_min_length"]
        self.assertGreater(cut["at"], 3.0)  # not at the 0.5s dip around 1.0-1.5s
        self.assertEqual(cut["reason"], "pause")

    def test_a_slice_of_pure_silence_is_flagged_so_it_is_never_sent(self):
        cuts = self.out["silence_only"]
        self.assertEqual([c["reason"] for c in cuts], ["max", "max"])
        self.assertFalse(any(c["hadSpeech"] for c in cuts))

    def test_speaking_from_the_very_first_moment_is_still_heard_as_speech(self):
        # The flaw in "calibrate on the first half-second": it would take the
        # speaker's own voice for the room and then never detect speech.
        (cut,) = self.out["speaks_immediately"]
        self.assertEqual(cut["reason"], "pause")
        self.assertTrue(cut["hadSpeech"])
        self.assertAlmostEqual(cut["at"], 2.9, delta=0.25)

    def test_a_noisy_room_is_not_mistaken_for_speech_but_real_speech_still_is(self):
        (cut,) = self.out["noisy_room"]
        self.assertEqual(cut["reason"], "pause")
        self.assertTrue(cut["hadSpeech"])
        self.assertAlmostEqual(cut["at"], 6.4, delta=0.3)  # after the speech, not during the noisy lead-in

    def test_a_quiet_speaker_with_natural_dips_is_cut_at_their_pause(self):
        (cut,) = self.out["quiet_speaker_with_natural_dips"]
        self.assertEqual(cut["reason"], "pause")
        self.assertTrue(cut["hadSpeech"])

    def test_a_quiet_speaker_is_never_discarded_even_if_they_cannot_be_told_from_the_room(self):
        # Speaking quietly from the first instant is indistinguishable from a
        # quiet room, so no pause is found - but the slice is still SENT (cut
        # at the maximum), because losing someone's words is worse than
        # transcribing a little silence.
        (cut,) = self.out["quiet_speaker_from_the_first_moment"]
        self.assertEqual(cut["reason"], "max")
        self.assertTrue(cut["hadSpeech"])

    def test_a_loud_continuous_talker_in_a_loud_room_is_not_dropped_as_silence(self):
        (cut,) = self.out["loud_continuous_talker_in_loud_room"]
        self.assertEqual(cut["reason"], "max")
        self.assertTrue(cut["hadSpeech"])

    def test_limits_are_configurable(self):
        self.assertEqual([round(c["at"]) for c in self.out["custom_limits"]], [4, 8])


class LiveSlicerWiringTests(unittest.TestCase):
    """The page must load the slicer (before app.js, which uses it) and the
    server must serve it and publish the limits it should use."""

    def test_index_loads_the_slicer_before_the_app_script(self):
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('src="/live_slicer.js"', html)
        self.assertLess(html.index('src="/live_slicer.js"'), html.index('src="/app.js"'))

    def test_server_serves_the_slicer_and_publishes_clamped_limits(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from unittest import mock

        from web_ui import DashboardHandler, DashboardRuntime

        previous = getattr(DashboardHandler, "runtime", None)
        DashboardHandler.runtime = DashboardRuntime()
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/live_slicer.js", timeout=3) as response:
                self.assertIn("javascript", response.headers["Content-Type"])
                self.assertIn(b"class LiveSlicer", response.read())
            with urllib.request.urlopen(base + "/api/status", timeout=3) as response:
                slicing = json.load(response)["settings"]["live_slicing"]
            self.assertEqual(slicing, {"min_ms": 2000, "max_ms": 8000, "pause_ms": 350})
            # an empty value (how Compose passes an unset variable) means "default"; absurd values are clamped
            env = {"POLYGLOT_LIVE_MIN_SLICE_SECONDS": "", "POLYGLOT_LIVE_MAX_SLICE_SECONDS": "999",
                   "POLYGLOT_LIVE_PAUSE_MS": "5"}
            with mock.patch.dict("os.environ", env):
                with urllib.request.urlopen(base + "/api/status", timeout=3) as response:
                    slicing = json.load(response)["settings"]["live_slicing"]
            self.assertEqual(slicing, {"min_ms": 2000, "max_ms": 15000, "pause_ms": 150})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if previous is not None:
                DashboardHandler.runtime = previous


if __name__ == "__main__":
    unittest.main()
