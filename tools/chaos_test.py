"""Chaos test: keep captions flowing while brokers and workers are killed and restarted at random.

    python tools/chaos_test.py --duration 120 --rate 1 --every 15

Against a running Docker Compose stack (project polyglot-relay). While --duration seconds of captions go out
at --rate per second, every --every seconds one random broker or worker container is killed (docker kill: no
clean shutdown) and started again a few seconds later. Meanwhile /metrics is read twice a second to record how
many brokers claim to lead. Afterwards it waits for stragglers and checks the two promises the relay makes:

* at-least-once delivery: every caption arrives in every language despite the failures;
* a single leader: never two leaders at the same time.

It also reports the longest time without any leader. Exit status 1 if either promise was broken.
Run it on a test stack: it really does kill containers. --dry-run lists what it would kill and kills nothing.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import load_test  # noqa: E402


def docker(*args: str) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def containers(project: str) -> list[str]:
    names = docker("ps", "--format", "{{.Names}}", "--filter", f"label=com.docker.compose.project={project}").splitlines()
    return sorted(n for n in names if re.search(r"-(broker-\d+|worker-[a-z0-9-]+)-\d+$", n))


class LeaderWatch(threading.Thread):
    """Reads polyglot_leader_count twice a second: the most leaders seen at once, and the longest gap with none."""

    def __init__(self, url: str, token: str = "", read=None) -> None:
        super().__init__(daemon=True)
        self.url, self.token = url, token
        self.read = read or self._read
        self.stop = threading.Event()
        self.most_leaders = 0
        self.longest_without = 0.0
        self.samples = 0
        self._without_since: float | None = None

    def _read(self) -> int | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=5) as response:
            for line in response.read().decode("utf-8").splitlines():
                if line.startswith("polyglot_leader_count "):
                    return int(float(line.split()[1]))
        return None

    def observe(self, leaders: int | None, now: float) -> None:
        if leaders is None:
            return
        self.samples += 1
        self.most_leaders = max(self.most_leaders, leaders)
        if leaders == 0:
            self._without_since = self._without_since or now
            self.longest_without = max(self.longest_without, now - self._without_since)
        else:
            self._without_since = None

    def run(self) -> None:
        while not self.stop.wait(0.5):
            try:
                self.observe(self.read(), time.monotonic())
            except (OSError, ValueError):
                pass


def chaos(victims: list[str], duration: float, every: float, down: tuple[float, float], rng: random.Random,
          kill, start, stop: threading.Event) -> list[dict]:
    """Kill a random victim every `every` seconds and start it again after a random pause in `down`."""
    events = []
    deadline = time.monotonic() + duration
    while not stop.is_set() and time.monotonic() + every < deadline:
        if stop.wait(every):
            break
        victim = rng.choice(victims)
        pause = rng.uniform(*down)
        kill(victim)
        events.append({"killed": victim, "down_seconds": round(pause, 1), "at": round(time.time(), 1)})
        stop.wait(pause)
        start(victim)
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--project", default="polyglot-relay")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--rooms", type=int, default=2)
    parser.add_argument("--every", type=float, default=15.0, help="seconds between failures")
    parser.add_argument("--down", type=float, nargs=2, default=(3.0, 10.0), help="seconds a victim stays down (range)")
    parser.add_argument("--only", choices=["brokers", "workers", "both"], default="both")
    parser.add_argument("--wait", type=float, default=180.0, help="seconds to wait for stragglers afterwards")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    victims = containers(args.project)
    if args.only != "both":
        victims = [v for v in victims if ("-broker-" in v) == (args.only == "brokers")]
    if not victims:
        print(f"No broker or worker containers found for project {args.project!r}; is the stack running?")
        return 1
    print(f"{len(victims)} possible victims: {', '.join(victims)}")
    if args.dry_run:
        return 0

    rng = random.Random(args.seed)
    stop = threading.Event()
    watch = LeaderWatch(args.url.rstrip("/") + "/metrics", args.token)
    watch.start()
    events: list[dict] = []
    breaker = threading.Thread(target=lambda: events.extend(chaos(
        victims, args.duration, args.every, tuple(args.down), rng,
        lambda name: docker("kill", name), lambda name: docker("start", name), stop)), daemon=True)
    breaker.start()
    count = max(1, int(args.duration * args.rate))
    report = load_test.run(load_test.Relay(args.url, args.token), count, args.rate, args.rooms, args.wait, label="chaos")
    stop.set()
    breaker.join(timeout=30)
    for victim in victims:                         # leave the stack whole
        docker("start", victim)
    watch.stop.set()
    report.update(failures=events, most_leaders_at_once=watch.most_leaders,
                  longest_without_a_leader_seconds=round(watch.longest_without, 1), leader_samples=watch.samples)
    load_test.print_report(report)
    print(f"{len(events)} failures injected: " + ", ".join(e["killed"] for e in events))
    print(f"Most leaders at once: {watch.most_leaders}; longest time without a leader: {watch.longest_without:.1f}s")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    broken = bool(report["missing"]) or watch.most_leaders > 1 or not report["sent"]
    if broken:
        print("RESULT: FAILED")
    else:
        print("RESULT: PASSED - every caption arrived in every language and there was never more than one leader")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
