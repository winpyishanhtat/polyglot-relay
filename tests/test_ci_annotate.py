"""tools/ci_annotate.py: unittest failures become GitHub annotations."""
import sys
import tempfile
import unittest
from pathlib import Path

import hermetic  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import ci_annotate  # noqa: E402

LOG = """..F.
======================================================================
FAIL: test_one (test_mod.Case.test_one)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/app/tests/test_mod.py", line 5, in test_one
    self.assertEqual(1, 2)
AssertionError: 1 != 2

======================================================================
ERROR: test_two (test_mod.Case.test_two)
----------------------------------------------------------------------
Traceback (most recent call last):
ValueError: 100% broken

----------------------------------------------------------------------
Ran 4 tests in 0.1s

FAILED (failures=1, errors=1)
"""


class AnnotateTests(unittest.TestCase):
    def test_each_failing_test_with_the_end_of_its_traceback(self):
        found = ci_annotate.failures(LOG)
        self.assertEqual([(k, t) for k, t, _ in found], [("FAIL", "test_mod.Case.test_one"), ("ERROR", "test_mod.Case.test_two")])
        self.assertIn("AssertionError: 1 != 2", found[0][2])
        self.assertNotIn("ValueError", found[0][2])

    def test_output_is_escaped_annotation_commands(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as handle:
            handle.write(LOG)
        import io
        from unittest import mock
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ci_annotate.main(["x", handle.name])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("::error title=FAIL test_mod.Case.test_one::"))
        self.assertIn("100%25 broken", lines[1])                  # % escaped
        self.assertNotIn("\n", lines[0].split("::", 2)[2])
        self.assertTrue(lines[-1].startswith("::notice title=unittest summary::Ran 4 tests"))
        Path(handle.name).unlink()


if __name__ == "__main__":
    unittest.main()
