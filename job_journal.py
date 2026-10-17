"""Unfinished translation jobs on disk, so they survive even every broker restarting at once.

Each broker keeps its own journal (POLYGLOT_JOB_JOURNAL, a SQLite file; on Docker a path under /data). The
leader writes a job when it is handed out and removes it when it is finished or given up; standbys mirror the
leader's journal through the replication they already receive. When a broker starts, what its journal still
holds is loaded as if it had been replicated to it, so whichever broker becomes leader delivers it again.
Jobs older than POLYGLOT_JOB_JOURNAL_MAX_AGE seconds (default 600) are not replayed: a caption that late is
no longer useful. Workers and the dashboard ignore duplicates, so replaying a job that was in fact finished
costs at most one extra translation.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

LOG = logging.getLogger("polyglot.journal")


def max_age() -> float:
    return max(1.0, float(os.getenv("POLYGLOT_JOB_JOURNAL_MAX_AGE") or "600"))


class JobJournal:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._failing = False
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS jobs (chunk_id TEXT NOT NULL, lang TEXT NOT NULL,
                chunk TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (chunk_id, lang))""")

    @classmethod
    def from_env(cls, broker_id: int) -> "JobJournal | None":
        """POLYGLOT_JOB_JOURNAL may contain {id}, so brokers sharing a volume use separate files."""
        template = os.getenv("POLYGLOT_JOB_JOURNAL") or ""
        if not template:
            return None
        path = template.replace("{id}", str(broker_id))
        try:
            return cls(path)
        except (OSError, sqlite3.Error) as exc:
            LOG.warning("Job journal %s is not usable (%s); unfinished jobs will not survive a full restart", path, exc)
            return None

    def _run(self, action: str, function, default=None):
        with self._lock:
            try:
                result = function()
                self._failing = False
                return result
            except sqlite3.Error as exc:
                if not self._failing:
                    LOG.warning("Job journal: cannot %s %s: %s", action, self.path, exc)
                self._failing = True
                return default

    def put(self, chunk: dict, langs: list[str], at: float | None = None) -> None:
        stamp = time.time() if at is None else at
        body = json.dumps(chunk, ensure_ascii=False)

        def write():
            with self._db:
                self._db.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?) ON CONFLICT (chunk_id, lang) DO NOTHING",
                                     [(str(chunk["chunk_id"]), lang, body, stamp) for lang in langs])
        self._run("write", write)

    def remove(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return

        def delete():
            with self._db:
                self._db.executemany("DELETE FROM jobs WHERE chunk_id = ? AND lang = ?", [(str(c), str(l)) for c, l in pairs])
        self._run("delete", delete)

    def load(self) -> list[tuple[str, str, dict, float]]:
        """Unfinished jobs young enough to be worth delivering, oldest first. Older ones are deleted."""
        cutoff = time.time() - max_age()

        def read():
            with self._db:
                self._db.execute("DELETE FROM jobs WHERE at < ?", (cutoff,))
            rows = self._db.execute("SELECT chunk_id, lang, chunk, at FROM jobs ORDER BY at").fetchall()
            return [(chunk_id, lang, json.loads(chunk), at) for chunk_id, lang, chunk, at in rows]
        return self._run("read", read, [])

    def count(self) -> int:
        return self._run("read", lambda: self._db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def close(self) -> None:
        with self._lock:
            self._db.close()
