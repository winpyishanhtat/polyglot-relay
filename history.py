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
