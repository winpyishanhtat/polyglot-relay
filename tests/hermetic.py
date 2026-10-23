"""Import this from a test module to keep the run hermetic: it removes any real Gemini API key from the
environment, so no test can reach the network (or spend quota) because the developer happens to have a
key set. Tests that need a key set one explicitly with mock.patch.dict.

It also removes the deployment settings a test run can inherit (the Compose test container gets the whole
stack's environment): with them, brokers created by different tests would share one on-disk job journal,
every message would need the cluster secret, and so on. Tests that exercise one of these set it themselves."""
import os

DEPLOYMENT_SETTINGS = (
    "POLYGLOT_JOB_JOURNAL", "POLYGLOT_JOB_JOURNAL_MAX_AGE", "POLYGLOT_RAFT_STATE", "POLYGLOT_ELECTION",
    "POLYGLOT_CLUSTER_SECRET", "POLYGLOT_HISTORY_FILE", "POLYGLOT_GLOSSARY_FILE", "POLYGLOT_TTS_CACHE_DIR",
    "POLYGLOT_TTS_DAILY_LIMIT", "POLYGLOT_INTERIM", "POLYGLOT_ACCESS_TOKEN", "POLYGLOT_VIEW_TOKEN",
    "POLYGLOT_FRAGMENT_GUARD", "POLYGLOT_CONTEXT_SENTENCES", "POLYGLOT_MAX_ROOMS",
)

for _name in list(os.environ):
    if _name in ("GEMINI_API_KEY", "GOOGLE_API_KEY") or _name.startswith("GEMINI_API_KEY_") or _name in DEPLOYMENT_SETTINGS:
        del os.environ[_name]
