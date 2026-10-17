"""Export formats for the caption history.

A history row is what DashboardState keeps for each spoken chunk:

    {"chunk_id", "number", "created_at", "text", "source_lang",
     "translations": {lang: {"text", "backend", "worker_id", "timestamp"}}}

Everything here is a pure function of rows, so it is easy to test and reuse.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence

from common import config


def clock(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def stamp(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def language_name(code: str | None) -> str:
    if code and code in config.LANGUAGES:
        return config.LANGUAGES[code]["name"]
    return "auto-detected" if not code or code == "auto" else code


def to_txt(rows: Sequence[dict], langs: Sequence[str]) -> str:
    """One block per chunk: time, number and spoken language, the original, then each translation."""
    blocks = []
    for row in rows:
        head = f"[{stamp(row['created_at'])}] #{row['number'] or '-'} ({language_name(row.get('source_lang'))})"
        lines = [head, f"  {row['text']}" if row.get("text") else "  (original not recorded)"]
        for lang in langs:
            item = row["translations"].get(lang)
            if item and not (lang == row.get("source_lang") and item["text"] == row.get("text")):   # not the original again
                lines.append(f"  {lang.upper()}: {item['text']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_csv(rows: Sequence[dict], langs: Sequence[str]) -> str:
    """Spreadsheet-friendly: number, time, spoken language, original, one column per language."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["number", "time", "spoken_language", "original", *langs])
    for row in rows:
        writer.writerow([row["number"] or "", stamp(row["created_at"]), row.get("source_lang") or "auto",
                         row.get("text") or "", *[(row["translations"].get(lang) or {}).get("text", "") for lang in langs]])
    return out.getvalue()


def to_json(rows: Sequence[dict], langs: Sequence[str]) -> str:
    kept = [{**row, "translations": {lang: row["translations"][lang] for lang in langs if lang in row["translations"]}}
            for row in rows]
    return json.dumps({"languages": list(langs), "chunks": kept}, ensure_ascii=False, indent=2) + "\n"


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    return f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"


def to_srt(rows: Sequence[dict], lang: str, max_seconds: float = 8.0, tail_seconds: float = 4.0,
           min_seconds: float = 1.5, chars_per_second: float = 15.0) -> str:
    """Subtitles in one language, timed from the first chunk. A caption stays up until the next one
    starts (at most max_seconds; the last stays tail_seconds). Chunks that arrive in a burst - an
    uploaded file, or a demo - would otherwise overlap or flash for milliseconds, so every cue lasts at
    least long enough to read (min_seconds, or its length at chars_per_second) and never starts before
    the previous one has ended. Chunks without that language are skipped."""
    items = [r for r in rows if lang in r["translations"]]
    if not items:
        return ""
    origin = items[0]["created_at"]
    blocks = []
    previous_end = 0.0
    for index, row in enumerate(items):
        text = row["translations"][lang]["text"]
        start = max(row["created_at"] - origin, previous_end)
        following = items[index + 1]["created_at"] - origin if index + 1 < len(items) else start + tail_seconds
        readable = min(max_seconds, max(min_seconds, len(text) / chars_per_second))
        end = max(start + readable, min(following, start + max_seconds))
        previous_end = end
        blocks.append(f"{index + 1}\n{srt_time(start)} --> {srt_time(end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


FORMATS = {"txt": ("text/plain; charset=utf-8", "txt"), "csv": ("text/csv; charset=utf-8", "csv"),
           "json": ("application/json; charset=utf-8", "json"), "srt": ("application/x-subrip; charset=utf-8", "srt")}


class HistoryStore:
    """Optional on-disk copy of the caption history (POLYGLOT_HISTORY_FILE), so a restart does not lose it.

    One JSON line per update of a row; a later line for the same chunk replaces an earlier one when the
    file is read back. The file is rewritten compactly once it holds many more lines than rows. A failing
    disk never stops captions: the first error is logged and writing is retried on the next update."""

    COMPACT_FACTOR = 4

    def __init__(self, path: str, max_rows: int, logger=None) -> None:
        self.path = path
        self.max_rows = max_rows
        self.log = logger
        self._lock = threading.Lock()
        self._lines = 0
        self._failing = False

    def load(self) -> "OrderedDict[str, dict]":
        rows: OrderedDict[str, dict] = OrderedDict()
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    self._lines += 1
                    try:
                        row = json.loads(line)
                        chunk_id = row["chunk_id"]
                        float(row["created_at"])
                        if not isinstance(row.get("translations"), dict):
                            raise ValueError("translations")
                    except (ValueError, KeyError, TypeError):
                        continue               # a torn last line after a crash, or hand editing: skip it
                    rows.pop(chunk_id, None)
                    rows[chunk_id] = row
        except FileNotFoundError:
            return rows
        except OSError as exc:
            self._report(f"cannot read {self.path}: {exc}")
            return rows
        ordered = sorted(rows.values(), key=lambda r: (r["created_at"], r.get("number") or 0))
        return OrderedDict((r["chunk_id"], r) for r in ordered[-self.max_rows:])

    def save(self, row: dict, rows: "Iterable[dict] | None" = None) -> None:
        """Append `row`. `rows` (every current row) is used only when the file needs compacting."""
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                if rows is not None and self._lines > self.COMPACT_FACTOR * self.max_rows:
                    self._rewrite(rows)
                else:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                    self._lines += 1
                self._failing = False
            except OSError as exc:
                self._report(f"cannot write {self.path}: {exc}")

    def wipe(self) -> None:
        with self._lock:
            try:
                open(self.path, "w", encoding="utf-8").close()
                self._lines = 0
            except OSError as exc:
                self._report(f"cannot clear {self.path}: {exc}")

    def _rewrite(self, rows: "Iterable[dict]") -> None:
        temporary = self.path + ".tmp"
        count = 0
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        os.replace(temporary, self.path)
        self._lines = count

    def _report(self, message: str) -> None:
        if not self._failing and self.log:
            self.log.warning("History file: %s", message)
        self._failing = True


SQLITE_MAGIC = b"SQLite format 3\x00"


class SqliteHistory:
    """The caption history on disk (POLYGLOT_HISTORY_FILE), one table for every room.

    Unlike memory, which keeps the newest POLYGLOT_HISTORY_CHUNKS per room, the database keeps up to
    POLYGLOT_HISTORY_DB_ROWS per room (default 100000), so the History panel can search far back. Writes are
    upserts keyed by (room, chunk_id). Like the JSON-lines file it replaces, a failing disk never stops
    captions: the first error is logged and every later call is tried again."""

    PRUNE_EVERY = 200

    def __init__(self, path: str, logger=None, max_rows: int | None = None) -> None:
        import sqlite3
        self.path = path
        self.log = logger
        self.max_rows = max_rows or max(100, int(os.getenv("POLYGLOT_HISTORY_DB_ROWS") or "100000"))
        self._lock = threading.Lock()
        self._failing = False
        self._saves = 0
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS captions (
                room TEXT NOT NULL, chunk_id TEXT NOT NULL, number INTEGER, created_at REAL NOT NULL,
                text TEXT, source_lang TEXT, translations TEXT NOT NULL, PRIMARY KEY (room, chunk_id))""")
            self._db.execute("CREATE INDEX IF NOT EXISTS captions_by_time ON captions (room, created_at)")

    @staticmethod
    def _row(record) -> dict:
        chunk_id, number, created_at, text, source_lang, translations = record
        return {"chunk_id": chunk_id, "number": number, "created_at": created_at, "text": text,
                "source_lang": source_lang, "translations": json.loads(translations)}

    def _run(self, action: str, function, default):
        with self._lock:
            try:
                result = function()
                self._failing = False
                return result
            except Exception as exc:  # noqa: BLE001 - a broken disk must never stop captions
                if not self._failing and self.log:
                    self.log.warning("History database: cannot %s %s: %s", action, self.path, exc)
                self._failing = True
                return default

    def save(self, room: str, row: dict) -> None:
        def write():
            with self._db:
                self._db.execute(
                    "INSERT INTO captions VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, chunk_id) DO UPDATE SET "
                    "number=excluded.number, created_at=excluded.created_at, text=excluded.text, "
                    "source_lang=excluded.source_lang, translations=excluded.translations",
                    (room, row["chunk_id"], row.get("number"), float(row["created_at"]), row.get("text"),
                     row.get("source_lang"), json.dumps(row.get("translations") or {}, ensure_ascii=False)))
            self._saves += 1
            if self._saves % self.PRUNE_EVERY == 0:
                self._prune(room)
        self._run("write", write, None)

    def _prune(self, room: str) -> None:
        with self._db:
            self._db.execute("""DELETE FROM captions WHERE room = ? AND chunk_id NOT IN (
                SELECT chunk_id FROM captions WHERE room = ? ORDER BY created_at DESC LIMIT ?)""", (room, room, self.max_rows))

    def load(self, room: str, limit: int) -> "OrderedDict[str, dict]":
        def read():
            records = self._db.execute(
                "SELECT chunk_id, number, created_at, text, source_lang, translations FROM captions WHERE room = ? "
                "ORDER BY created_at DESC, number DESC LIMIT ?", (room, limit)).fetchall()
            return OrderedDict((r["chunk_id"], r) for r in map(self._row, reversed(records)))
        return self._run("read", read, OrderedDict())

    def query(self, room: str, since: float | None, until: float | None) -> list[dict]:
        """Every row of a room in a time window, oldest first (filtering by language and text is the caller's)."""
        def read():
            sql = ("SELECT chunk_id, number, created_at, text, source_lang, translations FROM captions "
                   "WHERE room = ? AND created_at >= ? AND created_at <= ? ORDER BY created_at, number")
            low = float("-inf") if since is None else since
            high = float("inf") if until is None else until
            return [self._row(r) for r in self._db.execute(sql, (room, low, high))]
        return self._run("read", read, [])

    def count(self, room: str) -> int:
        return self._run("read", lambda: self._db.execute("SELECT COUNT(*) FROM captions WHERE room = ?", (room,)).fetchone()[0], 0)

    def rooms(self) -> list[str]:
        return self._run("read", lambda: [r[0] for r in self._db.execute("SELECT DISTINCT room FROM captions ORDER BY room")], [])

    def wipe(self, room: str) -> None:
        def clear():
            with self._db:
                self._db.execute("DELETE FROM captions WHERE room = ?", (room,))
        self._run("clear", clear, None)

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_history(path: str, logger=None, default_room: str = "main") -> SqliteHistory:
    """Open the history database at `path`. A JSON-lines history left by an earlier version - at `path` itself,
    or beside a new database as <name>.jsonl - is imported into `default_room` and kept as a .bak file."""
    legacy = None
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(SQLITE_MAGIC))
        if head and head != SQLITE_MAGIC:
            legacy = HistoryStore(path, 10 ** 9, logger).load()
            backup = path + ".jsonl.bak"
            os.replace(path, backup)
            if logger:
                logger.info("Moving %d captions from the old history file to SQLite (old file kept as %s)", len(legacy), backup)
    except FileNotFoundError:
        # A new database next to the JSON-lines file of an earlier version (history.db beside history.jsonl):
        # bring that history along, once.
        sibling = os.path.splitext(path)[0] + ".jsonl"
        if sibling != path and os.path.exists(sibling):
            legacy = HistoryStore(sibling, 10 ** 9, logger).load()
            os.replace(sibling, sibling + ".bak")
            if logger:
                logger.info("Moving %d captions from %s to %s (old file kept as %s.bak)", len(legacy), sibling, path, sibling)
    store = SqliteHistory(path, logger)
    for row in (legacy or {}).values():
        store.save(default_room, row)
    return store
