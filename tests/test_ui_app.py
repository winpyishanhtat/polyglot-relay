"""Runs the browser-side tests in tests/js with Node's built-in test runner."""

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class DashboardScriptTests(unittest.TestCase):
    def test_app_js_renders_correctly_against_a_fake_dom(self):
        result = subprocess.run(["node", "--test", os.path.join(ROOT, "tests", "js", "app.test.js")],
                                capture_output=True, text=True, timeout=120, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertRegex(result.stdout, r"# fail 0")
        self.assertNotRegex(result.stdout, r"# tests 0\b")


if __name__ == "__main__":
    unittest.main()
