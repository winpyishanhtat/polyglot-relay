import os
import unittest

import hermetic  # noqa: F401


class HermeticTests(unittest.TestCase):
    def test_no_real_gemini_key_is_visible_to_the_tests(self):
        leaked = [n for n in os.environ if n in ("GEMINI_API_KEY", "GOOGLE_API_KEY") or n.startswith("GEMINI_API_KEY_")]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
