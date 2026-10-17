"""tools/load_test.py and tools/chaos_test.py, against stubs and against a real mock relay."""

import io
import json
import os
import random
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import chaos_test  # noqa: E402
import load_test  # noqa: E402


def row(text, created, **delays):
    return {"text": text, "created_at": created,
            "translations": {lang: {"timestamp": created + d, "backend": "mock", "worker_id": f"{lang}-1"} for lang, d in delays.items()}}


class SummaryTests(unittest.TestCase):
    def test_complete_captions_delays_and_missing_languages(self):
        sent = [{"text": "a", "room": "r1", "at": 0}, {"text": "b", "room": "r2", "at": 1}, {"text": "c", "room": "r1", "at": 2}]
        rows = [row("a", 100, th=1.0, my=3.0), row("b", 101, th=2.0)]
        report = load_test.summarise(sent, rows, langs=("th", "my"), started=100, finished=103)
        self.assertEqual((report["sent"], report["complete"]), (3, 1))
        self.assertEqual(report["missing"], [{"text": "b", "room": "r2", "languages": ["my"]},
                                             {"text": "c", "room": "r1", "languages": ["th", "my"]}])
        self.assertEqual(report["delay_seconds"]["th"], {"median": 1.0, "p95": 2.0, "worst": 2.0, "count": 2})
        self.assertEqual(report["send_rate_per_second"], 1.0)

    def test_the_original_language_is_not_counted_as_a_delay(self):
        caption = row("a", 100, th=1.0)
        caption["translations"]["en"] = {"timestamp": 100, "backend": "passthrough", "worker_id": "source"}
        report = load_test.summarise([{"text": "a", "room": "r", "at": 0}], [caption], langs=("th", "en"), started=100, finished=100)
        self.assertEqual(report["delay_seconds"]["en"]["count"], 0)

    def test_percentiles(self):
        values = list(range(1, 101))
        self.assertEqual(load_test.percentile(values, 0.5), 50)
        self.assertEqual(load_test.percentile(values, 0.95), 95)
        self.assertIsNone(load_test.percentile([], 0.5))


class FakeRelay:
    def __init__(self, translate_langs=("th", "my"), refuse_every=0):
        self.captions = {}
        self.translate_langs = translate_langs
        self.refuse_every = refuse_every
        self.calls = 0

    def submit(self, text, room, lang="en"):
        self.calls += 1
        if self.refuse_every and self.calls % self.refuse_every == 0:
            return 429
        self.captions[text] = (room, time.time())
        return 202

    def history(self, room, needle):
        return [row(text, at, **{lang: 0.5 for lang in self.translate_langs})
                for text, (r, at) in self.captions.items() if r == room and needle in text]


class RunTests(unittest.TestCase):
    def test_a_healthy_relay_delivers_everything(self):
        relay = FakeRelay()
        report = load_test.run(relay, count=6, rate=200, rooms=3, wait=1, langs=("th", "my"))
        self.assertEqual((report["sent"], report["complete"], report["missing"]), (6, 6, []))
        self.assertEqual(len({room for room, _ in relay.captions.values()}), 3)

    def test_refused_captions_are_counted_and_a_missing_language_is_reported(self):
        relay = FakeRelay(translate_langs=("th",), refuse_every=3)
        report = load_test.run(relay, count=6, rate=200, rooms=1, wait=0.2, langs=("th", "my"))
        self.assertEqual(report["refused"], 2)
        self.assertEqual(len(report["missing"]), 4)
        self.assertEqual(report["missing"][0]["languages"], ["my"])

    def test_the_command_line_exit_status_reflects_missing_captions(self):
        with mock.patch.object(load_test, "run", return_value={**load_test.summarise([], []), "refused": 0, "sent": 2,
                                                                "missing": [{"text": "x"}]}), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(load_test.main(["--count", "2"]), 1)

    def test_bad_options_are_refused(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            load_test.main(["--rate", "0"])


class LeaderWatchTests(unittest.TestCase):
    def test_it_records_the_most_leaders_and_the_longest_gap(self):
        watch = chaos_test.LeaderWatch("http://x/metrics")
        for leaders, at in ((1, 0.0), (0, 1.0), (0, 2.5), (1, 3.0), (2, 4.0), (None, 5.0), (1, 6.0)):
            watch.observe(leaders, at)
        self.assertEqual(watch.most_leaders, 2)
        self.assertEqual(watch.longest_without, 1.5)
        self.assertEqual(watch.samples, 6)


class ChaosLoopTests(unittest.TestCase):
    def test_victims_are_killed_and_started_again(self):
        killed, started = [], []
        stop = threading.Event()
        events = chaos_test.chaos(["polyglot-relay-broker-3-1", "polyglot-relay-worker-th-1"], duration=0.35, every=0.05,
                                  down=(0.01, 0.02), rng=random.Random(3), kill=killed.append, start=started.append, stop=stop)
        self.assertTrue(events)
        self.assertEqual(killed, started)
        self.assertEqual([e["killed"] for e in events], killed)

    def test_it_stops_when_told(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(chaos_test.chaos(["a"], 10, 0.01, (0, 0), random.Random(), print, print, stop), [])

    def test_only_brokers_and_workers_are_victims(self):
        listing = "\n".join(["polyglot-relay-broker-1-1", "polyglot-relay-ui-1", "polyglot-relay-worker-en-1-1",
                             "polyglot-relay-worker-th-2", "polyglot-relay-libretranslate-1", "polyglot-relay-prometheus-1"])
        with mock.patch.object(chaos_test, "docker", return_value=listing):
            self.assertEqual(chaos_test.containers("polyglot-relay"),
                             ["polyglot-relay-broker-1-1", "polyglot-relay-worker-en-1-1", "polyglot-relay-worker-th-2"])

    def test_a_dry_run_kills_nothing(self):
        with mock.patch.object(chaos_test, "containers", return_value=["polyglot-relay-broker-1-1"]), \
                mock.patch.object(chaos_test, "docker") as docker, mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(chaos_test.main(["--dry-run"]), 0)
        docker.assert_not_called()

    def test_no_stack_is_a_clear_failure(self):
        with mock.patch.object(chaos_test, "containers", return_value=[]), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(chaos_test.main([]), 1)
        self.assertIn("is the stack running", out.getvalue())


class RealRelayLoadTests(unittest.TestCase):
    """The load test against real broker, worker and dashboard processes (mock translators, no keys)."""

    PORTS = (17731, 17732, 17733)

    def setUp(self):
        env = {**os.environ, "POLYGLOT_BROKER_1_HOST": "127.0.0.1", "POLYGLOT_BROKER_2_HOST": "127.0.0.1",
               "POLYGLOT_BROKER_1_PORT": str(self.PORTS[0]), "POLYGLOT_BROKER_2_PORT": str(self.PORTS[1]),
               "POLYGLOT_BROKER_COUNT": "2", "POLYGLOT_HEARTBEAT_INTERVAL": "0.2", "POLYGLOT_HEARTBEAT_TIMEOUT": "0.6",
               "POLYGLOT_ELECTION_TIMEOUT": "0.2", "POLYGLOT_CONNECT_TIMEOUT": "0.3", "POLYGLOT_PRELOAD_WHISPER": "0",
               "POLYGLOT_HISTORY_FILE": "", "POLYGLOT_RATE_LIMIT_PER_MINUTE": "0", "POLYGLOT_CONTEXT_SENTENCES": "2"}
        from common.config import TARGET_LANGUAGES
        commands = [["broker.py", "--id", "1"], ["broker.py", "--id", "2"]]
        commands += [["translator_worker.py", "--lang", lang, "--mock"] for lang in TARGET_LANGUAGES]
        commands += [["web_ui.py", "--port", str(self.PORTS[2])]]
        self.processes = [subprocess.Popen([sys.executable, *c], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL) for c in commands]
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.PORTS[2]}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/api/status", timeout=2) as response:
                    if all(w["connected"] for w in json.load(response)["workers"].values()):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        self.fail("the relay did not come up")

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def test_every_caption_arrives_in_every_language_across_rooms(self):
        report = load_test.run(load_test.Relay(self.base), count=24, rate=8, rooms=3, wait=30)
        self.assertEqual(report["missing"], [], report["missing"][:3])
        self.assertEqual(report["complete"], 24)
        self.assertIsNotNone(report["delay_seconds"]["th"]["median"])


if __name__ == "__main__":
    unittest.main()
