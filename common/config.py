"""Configuration shared by every Polyglot Relay process.

Environment variables make it possible to run several isolated test clusters
without editing source code.
"""

from __future__ import annotations

import os


BROKERS = [
    {"id": 1, "host": os.getenv("POLYGLOT_BROKER_1_HOST", "127.0.0.1"), "port": int(os.getenv("POLYGLOT_BROKER_1_PORT", "7001"))},
    {"id": 2, "host": os.getenv("POLYGLOT_BROKER_2_HOST", "127.0.0.1"), "port": int(os.getenv("POLYGLOT_BROKER_2_PORT", "7002"))},
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
HEARTBEAT_INTERVAL = float(os.getenv("POLYGLOT_HEARTBEAT_INTERVAL", "2"))
HEARTBEAT_TIMEOUT = float(os.getenv("POLYGLOT_HEARTBEAT_TIMEOUT", "6"))
ELECTION_TIMEOUT = float(os.getenv("POLYGLOT_ELECTION_TIMEOUT", "2"))
CONNECT_TIMEOUT = float(os.getenv("POLYGLOT_CONNECT_TIMEOUT", "1.5"))
RECONNECT_DELAY = float(os.getenv("POLYGLOT_RECONNECT_DELAY", "1"))
WORKER_STALE_TIMEOUT = float(os.getenv("POLYGLOT_WORKER_STALE_TIMEOUT", "7"))
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
