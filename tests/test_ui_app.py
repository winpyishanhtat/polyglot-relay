"""Runs the browser-side tests in tests/js with Node's built-in test runner."""

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class DashboardScriptTests(unittest.TestCase):
    def run_node_tests(self, name):
        result = subprocess.run(["node", "--test", os.path.join(ROOT, "tests", "js", name)],
                                capture_output=True, text=True, timeout=120, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertRegex(result.stdout, r"# fail 0")
        self.assertNotRegex(result.stdout, r"# tests 0\b")

    def test_app_js_renders_correctly_against_a_fake_dom(self):
        self.run_node_tests("app.test.js")

    def test_speech_js_chooses_between_the_browser_voice_and_the_relay(self):
        self.run_node_tests("speech.test.js")

    def test_listen_js_merges_and_orders_captions(self):
        self.run_node_tests("listen.test.js")


if __name__ == "__main__":
    unittest.main()
