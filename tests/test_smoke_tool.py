"""tools/smoke_test.py: it must pass on a healthy cluster and say exactly what is wrong on a broken one."""

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import smoke_test  # noqa: E402

LANGS = ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"]


class StubRelay:
    """A tiny HTTP server that plays a relay: `routes` maps (method, path-prefix) to (status, body)."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((method, self.path, dict(self.headers), body))
                answer = None
                for (m, prefix), value in outer.routes.items():
                    if m == method and self.path.split("?")[0] == prefix.split("?")[0]:
                        answer = value() if callable(value) else value
                        break
                status, payload, kind = answer if answer and len(answer) == 3 else (*(answer or (404, {})), "json")
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if kind == "json" else "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self.handle_any("GET")

            def do_POST(self):  # noqa: N802
                self.handle_any("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def healthy_routes(translations=None, workers=None, brokers=None, leader_count=1):
    translations = LANGS if translations is None else translations
    sent = {}
    brokers = brokers or [{"id": n, "online": True, "leader": n == 3 and leader_count >= 1 or (leader_count == 2 and n == 2)} for n in (1, 2, 3)]

    def history():
        row = {"chunk_id": "x", "number": 4, "text": sent.get("text"), "source_lang": "en",
               "translations": {lang: {"text": "t"} for lang in translations}} if sent else None
        return 200, {"chunks": [row] if row else []}

    def caption():
        return 202, {"accepted": True}

    stub_routes = {
        ("GET", "/health"): (200, {"status": "ok"}),
        ("GET", "/api/languages"): (200, {"languages": [{"code": c} for c in LANGS]}),
        ("GET", "/api/status"): (200, {"brokers": brokers,
                                       "workers": workers or {c: {"connected": True} for c in LANGS}}),
        ("GET", "/"): (200, b"<html>", "text"), ("GET", "/listen"): (200, b"<html>", "text"),
        ("GET", "/listen/en"): (200, b"<html>", "text"), ("GET", "/speech.js"): (200, b"js", "text"),
        ("GET", "/listen.js"): (200, b"js", "text"),
        ("GET", "/api/history"): history,
        ("POST", "/api/captions"): caption,
    }
    return stub_routes, sent


class SmokeCheckTests(unittest.TestCase):
    def serve(self, routes):
        stub = StubRelay(routes)
        self.addCleanup(stub.close)
        return stub

    def results(self, stub, **kwargs):
        return {r.name: r for r in smoke_test.run_checks(stub.url, wait=kwargs.pop("wait", 0.3), **kwargs)}

    def test_nothing_listening_fails_every_check_and_raises_nothing(self):
        refused = ConnectionRefusedError("connection refused")      # simulated: a real refusal is slow on Windows
        with mock.patch.object(smoke_test.Relay, "call", side_effect=refused):
            results = smoke_test.run_checks("http://127.0.0.1:9", wait=0.5)
        self.assertGreaterEqual(len(results), 5)
        self.assertTrue(all(not r.ok for r in results))
        self.assertTrue(all("ConnectionRefusedError" in r.detail for r in results))

    def test_a_missing_worker_is_named(self):
        routes, sent = healthy_routes(workers={**{c: {"connected": True} for c in LANGS}, "km": {"connected": False}, "lo": {"connected": False}})
        results = self.results(self.serve(routes))
        failing = results["a worker is connected for every language"]
        self.assertFalse(failing.ok)
        self.assertIn("km", failing.detail)
        self.assertIn("lo", failing.detail)

    def test_two_leaders_or_none_fails_the_leader_check(self):
        for count, expect in ((0, "[]"), (2, "[2, 3]")):
            routes, _ = healthy_routes(leader_count=count)
            result = self.results(self.serve(routes))["exactly one broker leads"]
            self.assertFalse(result.ok, count)
            self.assertIn(expect, result.detail)

    def test_a_language_that_never_arrives_is_reported_with_what_did(self):
        routes, sent = healthy_routes(translations=["en", "th"])

        def caption():
            return 202, {"accepted": True}
        routes[("POST", "/api/captions")] = caption
        stub = self.serve(routes)
        original = stub.routes[("POST", "/api/captions")]

        def remember():
            body = json.loads(stub.requests[-1][3])
            sent["text"] = body["text"]
            return original()
        stub.routes[("POST", "/api/captions")] = remember
        results = self.results(stub, wait=1.0)
        arrival = results["every language is translated"]
        self.assertFalse(arrival.ok)
        self.assertIn("['en', 'th']", arrival.detail)

    def test_every_check_runs_even_after_an_earlier_one_fails(self):
        routes, _ = healthy_routes(workers={c: {"connected": False} for c in LANGS})
        names = [r.name for r in smoke_test.run_checks(self.serve(routes).url, wait=0.5)]
        self.assertIn("pages and listener routes are served", names)
        self.assertIn("a typed caption is accepted", names)

    def test_a_refused_caption_skips_the_checks_that_depend_on_it(self):
        routes, _ = healthy_routes()
        routes[("POST", "/api/captions")] = (401, {"error": "Access token required"})
        results = smoke_test.run_checks(self.serve(routes).url, wait=0.5)
        names = [r.name for r in results]
        self.assertIn("a typed caption is accepted", names)
        self.assertNotIn("every language is translated", names)
        refused = next(r for r in results if r.name == "a typed caption is accepted")
        self.assertFalse(refused.ok)
        self.assertIn("401", refused.detail)

    def test_the_token_is_sent_with_every_request(self):
        routes, _ = healthy_routes()
        stub = self.serve(routes)
        smoke_test.run_checks(stub.url, token="s3cret", wait=0.5)
        self.assertTrue(stub.requests)
        self.assertTrue(all(headers.get("X-Access-Token") == "s3cret" for _, _, headers, _ in stub.requests))

    def test_speech_is_only_tested_when_asked_and_must_return_a_wav(self):
        routes, _ = healthy_routes()
        routes[("POST", "/api/tts")] = (200, b"RIFF....WAVE", "text")
        stub = self.serve(routes)
        without = [r.name for r in smoke_test.run_checks(stub.url, wait=0.5)]
        self.assertNotIn("read-aloud returns audio", without)
        self.assertFalse(any(path == "/api/tts" for _, path, _, _ in stub.requests))
        with_speech = {r.name: r for r in smoke_test.run_checks(stub.url, wait=0.5, speech=True)}
        self.assertTrue(with_speech["read-aloud returns audio"].ok)
        routes[("POST", "/api/tts")] = (200, b"not audio", "text")
        self.assertFalse({r.name: r for r in smoke_test.run_checks(stub.url, wait=0.5, speech=True)}["read-aloud returns audio"].ok)

    def test_a_page_that_returns_the_wrong_status_is_named(self):
        routes, _ = healthy_routes()
        routes[("GET", "/listen")] = (500, b"boom", "text")
        result = self.results(self.serve(routes))["pages and listener routes are served"]
        self.assertFalse(result.ok)
        self.assertIn("/listen gave 500", result.detail)

    def test_an_unknown_listener_language_must_be_a_404(self):
        routes, _ = healthy_routes()
        routes[("GET", "/listen/zz")] = (200, b"<html>", "text")
        result = self.results(self.serve(routes))["pages and listener routes are served"]
        self.assertIn("/listen/zz gave 200, expected 404", result.detail)


class CommandLineTests(unittest.TestCase):
    def test_exit_status_is_zero_only_when_every_check_passed(self):
        passing = [smoke_test.Check("a", True, "fine"), smoke_test.Check("b", True)]
        failing = passing + [smoke_test.Check("c", False, "broken")]
        for results, expected in ((passing, 0), (failing, 1)):
            with mock.patch.object(smoke_test, "run_checks", return_value=results), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(smoke_test.main(["--url", "http://x"]), expected)
        self.assertIn("FAIL  c  (broken)", out.getvalue())
        self.assertIn("2 of 3 checks passed", out.getvalue())

    def test_the_options_reach_the_checks(self):
        with mock.patch.object(smoke_test, "run_checks", return_value=[]) as run, mock.patch("sys.stdout", new_callable=io.StringIO):
            smoke_test.main(["--url", "http://relay:8000", "--token", "t", "--wait", "5", "--speech"])
        run.assert_called_once_with("http://relay:8000", "t", 5.0, True)


class AgainstARealClusterTests(unittest.TestCase):
    """The same checks against real broker, worker and dashboard processes (mock translators, no keys)."""

    PORTS = (17701, 17702, 17703)

    def setUp(self):
        env = os.environ.copy()
        env.update(POLYGLOT_BROKER_1_HOST="127.0.0.1", POLYGLOT_BROKER_2_HOST="127.0.0.1",
                   POLYGLOT_BROKER_1_PORT=str(self.PORTS[0]), POLYGLOT_BROKER_2_PORT=str(self.PORTS[1]),
                   POLYGLOT_BROKER_COUNT="2", POLYGLOT_HEARTBEAT_INTERVAL="0.2", POLYGLOT_HEARTBEAT_TIMEOUT="0.6",
                   POLYGLOT_ELECTION_TIMEOUT="0.2", POLYGLOT_CONNECT_TIMEOUT="0.3",
                   POLYGLOT_PRELOAD_WHISPER="0", POLYGLOT_HISTORY_FILE="", POLYGLOT_VIEW_TOKEN="", POLYGLOT_ACCESS_TOKEN="")
        commands = [["broker.py", "--id", "1"], ["broker.py", "--id", "2"]]
        from common.config import TARGET_LANGUAGES
        commands += [["translator_worker.py", "--lang", lang, "--mock"] for lang in TARGET_LANGUAGES]
        commands += [["web_ui.py", "--port", str(self.PORTS[2])]]
        self.processes = [subprocess.Popen([sys.executable, *command], cwd=ROOT, env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for command in commands]
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.PORTS[2]}"

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def wait_until_ready(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/api/status", timeout=2) as response:
                    status = json.load(response)
                if all(w["connected"] for w in status["workers"].values()) and any(b["leader"] for b in status["brokers"] if b["online"]):
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.2)
        self.fail("the cluster did not come up")

    def test_it_passes_on_a_healthy_cluster_then_fails_and_names_the_worker_that_was_lost(self):
        self.wait_until_ready()
        results = smoke_test.run_checks(self.base, wait=20)
        self.assertEqual([r.name for r in results if not r.ok], [], [(r.name, r.detail) for r in results])
        self.assertEqual(len(results), 9)

        victim = self.processes[2 + 4]                               # the worker for the fifth language (Thai)
        victim.terminate()
        victim.wait(timeout=5)
        deadline = time.time() + 15
        while time.time() < deadline:
            with urllib.request.urlopen(self.base + "/api/status", timeout=2) as response:
                if not all(w["connected"] for w in json.load(response)["workers"].values()):
                    break
            time.sleep(0.3)
        broken = {r.name: r for r in smoke_test.run_checks(self.base, wait=3)}
        self.assertFalse(broken["a worker is connected for every language"].ok)
        self.assertIn("th", broken["a worker is connected for every language"].detail)
        self.assertFalse(broken["every language is translated"].ok)      # Thai never arrives
        self.assertTrue(broken["exactly one broker leads"].ok)           # the rest is still healthy


if __name__ == "__main__":
    unittest.main()
