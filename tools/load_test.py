"""Load test: how many captions the relay carries, how fast, and whether any go missing.

    python tools/load_test.py --rate 2 --count 120 --rooms 4

It sends --count typed captions at --rate per second, spread over --rooms rooms (each room allows 100
unfinished captions, so rooms let the load go higher), then waits up to --wait seconds for every caption to
arrive in every language and reports:

* throughput: captions sent per second and fully translated per second;
* delay per language: median, 95th percentile and worst, from the sentence being recorded to its translation
  arriving (both times taken by the relay, so the network to this script does not count);
* anything missing: captions or languages that never arrived.

The dashboard allows POLYGLOT_RATE_LIMIT_PER_MINUTE posts per client (240 by default), so --rate above 4 needs
that raised. With mock translators (compose.yaml alone) it measures the relay itself; with real translators it
also measures LibreTranslate and Gemini, and spends their quota. Exit status 1 if anything went missing.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LANGS = ("en", "my", "id", "ms", "th", "vi", "tl", "km", "lo")


class Relay:
    def __init__(self, base: str, token: str = "") -> None:
        self.base, self.token = base.rstrip("/"), token

    def _request(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Access-Token"] = self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read() or b"{}")
            except ValueError:
                return error.code, {}

    def submit(self, text: str, room: str, lang: str = "en") -> int:
        return self._request(f"/api/captions?room={urllib.parse.quote(room)}", {"text": text, "source_lang": lang})[0]

    def history(self, room: str, needle: str) -> list[dict]:
        query = urllib.parse.urlencode({"room": room, "q": needle, "limit": 1000, "last_minutes": 120})
        status, body = self._request(f"/api/history?{query}")
        return body.get("chunks", []) if status == 200 else []


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def summarise(sent: list[dict], rows: list[dict], langs=LANGS, started: float = 0.0, finished: float = 0.0) -> dict:
    """sent: [{"text", "room", "at"}]; rows: the history rows found for them."""
    by_text = {row.get("text"): row for row in rows}
    delays: dict[str, list[float]] = {lang: [] for lang in langs}
    missing: list[dict] = []
    complete = 0
    last_arrival = started
    for item in sent:
        row = by_text.get(item["text"])
        if not row:
            missing.append({"text": item["text"], "room": item["room"], "languages": list(langs)})
            continue
        absent = [lang for lang in langs if lang not in row.get("translations", {})]
        if absent:
            missing.append({"text": item["text"], "room": item["room"], "languages": absent})
        else:
            complete += 1
        for lang, translation in row.get("translations", {}).items():
            if lang in delays and translation.get("backend") != "passthrough" and translation.get("worker_id") != "source":
                delays[lang].append(max(0.0, float(translation.get("timestamp", row["created_at"])) - float(row["created_at"])))
                last_arrival = max(last_arrival, float(translation.get("timestamp", 0)))
    sending = max(1e-9, finished - started)
    return {
        "sent": len(sent),
        "complete": complete,
        "missing": missing,
        "send_rate_per_second": round(len(sent) / sending, 2),
        "completed_per_second": round(complete / max(1e-9, last_arrival - started), 2) if complete else 0.0,
        "delay_seconds": {lang: {"median": _round(percentile(v, 0.5)), "p95": _round(percentile(v, 0.95)),
                                 "worst": _round(max(v) if v else None), "count": len(v)} for lang, v in delays.items()},
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def run(relay: Relay, count: int, rate: float, rooms: int, wait: float, langs=LANGS, label: str = "load",
        stop: threading.Event | None = None) -> dict:
    marker = f"{label}-{uuid.uuid4().hex[:6]}"
    room_names = [f"{label}-{n}" for n in range(1, rooms + 1)]
    sent: list[dict] = []
    refused = 0
    started = time.time()
    for n in range(count):
        if stop is not None and stop.is_set():
            break
        room = room_names[n % len(room_names)]
        text = f"{marker} caption {n} about the weather and the meeting at nine"
        status = relay.submit(text, room)
        if status == 202:
            sent.append({"text": text, "room": room, "at": time.time()})
        else:
            refused += 1
        time.sleep(max(0.0, started + (n + 1) / rate - time.time()))
    finished = time.time()
    rows: list[dict] = []
    deadline = time.time() + wait
    while True:
        rows = [row for room in room_names for row in relay.history(room, marker)]
        report = summarise(sent, rows, langs, started, finished)
        if not report["missing"] or time.time() > deadline:
            break
        time.sleep(1.0)
    report["refused"] = refused
    report["rooms"] = room_names
    report["marker"] = marker
    return report


def print_report(report: dict) -> None:
    print(f"Sent {report['sent']} captions ({report['send_rate_per_second']}/s), {report['refused']} refused; "
          f"{report['complete']} arrived in every language ({report['completed_per_second']}/s).")
    print("| Language | Median s | 95th pct s | Worst s | Captions |\n|---|---|---|---|---|")
    for lang, row in report["delay_seconds"].items():
        print(f"| {lang} | {row['median']} | {row['p95']} | {row['worst']} | {row['count']} |")
    if report["missing"]:
        print(f"MISSING: {len(report['missing'])} captions incomplete, e.g. {report['missing'][:3]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--rate", type=float, default=1.0, help="captions per second")
    parser.add_argument("--rooms", type=int, default=2)
    parser.add_argument("--wait", type=float, default=120.0, help="seconds to wait for stragglers")
    parser.add_argument("--out", help="also write the report as JSON here")
    args = parser.parse_args(argv)
    if args.rate <= 0 or args.count <= 0 or args.rooms <= 0:
        parser.error("--rate, --count and --rooms must be positive")
    report = run(Relay(args.url, args.token), args.count, args.rate, args.rooms, args.wait)
    print_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 1 if report["missing"] or not report["sent"] else 0


if __name__ == "__main__":
    sys.exit(main())
