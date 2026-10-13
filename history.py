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
import time
from collections.abc import Sequence

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
