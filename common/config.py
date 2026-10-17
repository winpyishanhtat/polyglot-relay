"""Configuration shared by every Polyglot Relay process.

Environment variables make it possible to run several isolated test clusters
without editing source code.
"""

from __future__ import annotations

import os


# How many broker replicas the cluster runs (ids 1..N; the highest live id leads - Bully election).
# Two is the minimum for failover; three survives losing two in turn. Empty means the default.
BROKER_COUNT = max(1, min(7, int(os.getenv("POLYGLOT_BROKER_COUNT") or "2")))
BROKERS = [
    {"id": number, "host": os.getenv(f"POLYGLOT_BROKER_{number}_HOST") or "127.0.0.1",
     "port": int(os.getenv(f"POLYGLOT_BROKER_{number}_PORT") or str(7000 + number))}
    for number in range(1, BROKER_COUNT + 1)
]

# Single source of truth for every language the relay can translate into.
# ASEAN-focused build: English (ASEAN's official working language, covers
# Singapore) plus one representative language per remaining ASEAN member
# state (Malay covers Brunei). "group" drives UI grouping/badges only; it
# has no effect on routing - every entry shares "ASEAN" now that the
# project no longer targets non-ASEAN languages.
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"name": "English", "native": "English", "group": "ASEAN"},
    "my": {"name": "Burmese", "native": "မြန်မာ", "group": "ASEAN"},
    "id": {"name": "Indonesian", "native": "Bahasa Indonesia", "group": "ASEAN"},
    "ms": {"name": "Malay", "native": "Bahasa Melayu", "group": "ASEAN"},
    "th": {"name": "Thai", "native": "ไทย", "group": "ASEAN"},
    "vi": {"name": "Vietnamese", "native": "Tiếng Việt", "group": "ASEAN"},
    "tl": {"name": "Filipino", "native": "Filipino", "group": "ASEAN"},
    "km": {"name": "Khmer", "native": "ខ្មែរ", "group": "ASEAN"},
    "lo": {"name": "Lao", "native": "ລາວ", "group": "ASEAN"},
}
TARGET_LANGUAGES = list(LANGUAGES.keys())
AUDIO_CHUNK_SECONDS = float(os.getenv("POLYGLOT_AUDIO_CHUNK_SECONDS", "2.5"))
# Failure-detection timing. Measured with eval.failover_trials (time from killing the leader to a new one):
#   interval 2 / timeout 6 / election 2 / connect 1.5 -> 8.25 s   (the old defaults)
#   interval 1 / timeout 3 / election 1 / connect 1   -> 4.77 s   (these defaults: three missed heartbeats)
#   interval 0.5 / timeout 2 / election 1 / connect .75 -> 3.01 s  (faster, but four missed beats of 0.5 s
#                                                          can misfire when the host is starved of CPU)
# Empty or unset environment values mean the default.
HEARTBEAT_INTERVAL = float(os.getenv("POLYGLOT_HEARTBEAT_INTERVAL") or "1")
HEARTBEAT_TIMEOUT = float(os.getenv("POLYGLOT_HEARTBEAT_TIMEOUT") or "3")
ELECTION_TIMEOUT = float(os.getenv("POLYGLOT_ELECTION_TIMEOUT") or "1")
CONNECT_TIMEOUT = float(os.getenv("POLYGLOT_CONNECT_TIMEOUT") or "1")
RECONNECT_DELAY = float(os.getenv("POLYGLOT_RECONNECT_DELAY", "1"))
WORKER_STALE_TIMEOUT = float(os.getenv("POLYGLOT_WORKER_STALE_TIMEOUT") or "4")
DEFAULT_WORKER_HOST = os.getenv("POLYGLOT_WORKER_HOST", "127.0.0.1")
DEFAULT_WORKER_BIND_HOST = os.getenv("POLYGLOT_WORKER_BIND_HOST", DEFAULT_WORKER_HOST)
DEFAULT_WORKER_PORT_BASE = int(os.getenv("POLYGLOT_WORKER_PORT_BASE", "8100"))
BROKER_BIND_HOST = os.getenv("POLYGLOT_BROKER_BIND_HOST")


def broker_by_id(broker_id: int) -> dict:
    """Return a copy of a broker configuration by numeric ID."""
    for broker in BROKERS:
        if broker["id"] == broker_id:
            return dict(broker)
    raise ValueError(f"Unknown broker id {broker_id}; expected one of {[b['id'] for b in BROKERS]}")


def language_label(code: str) -> str:
    """Human-readable label for translation prompts, e.g. 'Thai (ไทย)'."""
    meta = LANGUAGES[code]
    name, native = meta["name"], meta["native"]
    return name if native == name else f"{name} ({native})"


# Leader election: "bully" (default; the highest live broker leads, so even a single survivor carries on) or
# "raft" (terms and majority votes; never two leaders even when the network splits, but needs a majority up).
ELECTION_MODE = (os.getenv("POLYGLOT_ELECTION") or "bully").strip().lower()
if ELECTION_MODE not in ("bully", "raft"):
    raise ValueError("POLYGLOT_ELECTION must be bully or raft")
