"""Check a running Polyglot Relay end to end: python tools/smoke_test.py [--url http://localhost:8000]

It sends one typed caption through the real pipeline and waits for every language to come back, then checks the
pages, the history and the export. Nothing is changed except that one caption appears in the history (use
"Clear captions & history" on the dashboard to remove it). Exit status is 0 only when every check passed.
Speech (--speech) is off by default because each call spends Gemini quota.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class Relay:
    def __init__(self, base: str, token: str = "", timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, path: str, body: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Access-Token"] = self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def json(self, path: str, body: dict | None = None):
        status, _, payload = self.call(path, body)
        return status, json.loads(payload or b"{}")


def run_checks(base: str, token: str = "", wait: float = 60.0, speech: bool = False) -> list[Check]:
    """Every check runs even when an earlier one fails; a failure is a result, never an exception."""
    relay = Relay(base, token)
    results: list[Check] = []

    def check(name, function):
        try:
            ok, detail = function()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check, with its reason
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(Check(name, bool(ok), str(detail)))
        return ok

    languages: list[str] = []

    def health():
        status, body = relay.json("/health")
        return status == 200 and body.get("status") == "ok", f"HTTP {status}"

    def languages_listed():
        status, body = relay.json("/api/languages")
        languages.extend(entry["code"] for entry in body.get("languages", []))
        return status == 200 and len(languages) >= 2, f"{len(languages)} languages"

    def one_leader():
        status, body = relay.json("/api/status")
        leaders = [b["id"] for b in body.get("brokers", []) if b.get("online") and b.get("leader")]
        online = [b["id"] for b in body.get("brokers", []) if b.get("online")]
        return status == 200 and len(leaders) == 1, f"leader {leaders}, online {online}"

    def workers_connected():
        status, body = relay.json("/api/status")
        missing = [lang for lang, w in body.get("workers", {}).items() if not w.get("connected")]
        return status == 200 and body.get("workers") and not missing, "all connected" if not missing else f"no worker for {missing}"

    def pages():
        problems = []
        for path, expected in (("/", 200), ("/listen", 200), ("/listen/" + (languages[0] if languages else "en"), 200),
                               ("/listen/zz", 404), ("/speech.js", 200), ("/listen.js", 200)):
            status = relay.call(path)[0]
            if status != expected:
                problems.append(f"{path} gave {status}, expected {expected}")
        return not problems, "; ".join(problems) or "dashboard and listener pages served"

    marker = f"smoke test {uuid.uuid4().hex[:8]}"
    submitted = {}

    def submit():
        status, body = relay.json("/api/captions", {"text": marker, "source_lang": "en"})
        submitted["at"] = time.time()
        return status == 202 and body.get("accepted"), f"HTTP {status} {body.get('error', '')}".strip()

    def row():
        history = relay.json("/api/history?last_minutes=10&limit=200")[1].get("chunks", [])
        return next((r for r in history if r.get("text") == marker), None)

    def every_language_arrives():
        deadline = time.time() + wait
        found = None
        while time.time() < deadline:
            found = row()
            if found and all(lang in found["translations"] for lang in languages):
                return True, f"all {len(languages)} languages in {time.time() - submitted['at']:.1f}s"
            time.sleep(0.5)
        have = sorted(found["translations"]) if found else []
        return False, f"after {wait:.0f}s only {have} had arrived"

    def history_is_complete():
        found = row()
        ok = bool(found) and bool(found.get("number")) and found.get("source_lang") == "en"
        return ok, f"chunk #{found.get('number') if found else '-'}"

    def export_has_it():
        status, headers, payload = relay.call("/api/export?format=txt&last_minutes=10")
        return status == 200 and marker in payload.decode("utf-8", "replace"), f"HTTP {status}"

    def speech_works():
        status, headers, payload = relay.call("/api/tts", {"text": "hello", "lang": "en"})
        return status == 200 and payload[:4] == b"RIFF", f"HTTP {status}, {len(payload)} bytes"

    check("health endpoint answers", health)
    check("languages are listed", languages_listed)
    check("exactly one broker leads", one_leader)
    check("a worker is connected for every language", workers_connected)
    check("pages and listener routes are served", pages)
    if check("a typed caption is accepted", submit):
        check("every language is translated", every_language_arrives)
        check("the history records it with its number", history_is_complete)
        check("the export contains it", export_has_it)
    if speech:
        check("read-aloud returns audio", speech_works)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default="", help="the access/viewing token, if the dashboard has one")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for all translations")
    parser.add_argument("--speech", action="store_true", help="also test /api/tts (spends Gemini speech quota)")
    args = parser.parse_args(argv)
    results = run_checks(args.url, args.token, args.wait, args.speech)
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}" + (f"  ({result.detail})" if result.detail else ""))
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
