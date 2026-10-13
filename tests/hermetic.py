"""Import this from a test module to keep the run hermetic: it removes any real Gemini API key from the
environment, so no test can reach the network (or spend quota) because the developer happens to have a
key set. Tests that need a key set one explicitly with mock.patch.dict."""
import os

for _name in list(os.environ):
    if _name in ("GEMINI_API_KEY", "GOOGLE_API_KEY") or _name.startswith("GEMINI_API_KEY_"):
        del os.environ[_name]
