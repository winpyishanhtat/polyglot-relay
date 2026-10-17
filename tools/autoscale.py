"""Grow or shrink each language's worker pool from its queue length.

    python tools/autoscale.py --compose compose.yaml --compose compose.libretranslate.yaml
    python tools/autoscale.py --dry-run          # only print what it would do

Every --interval seconds it reads the dashboard's /metrics (workers and queue depth per language, plus jobs
waiting for a worker) and decides, per language:

* scale up by one when the backlog per worker has been above --high for two readings in a row (one busy
  reading is a burst, not a trend), or at once when there is work and no worker at all;
* scale down by one when the pool has had no backlog for --cooldown seconds;
* never outside --min .. --max workers, and never more often than once per --cooldown per language.

Scaling runs `docker compose ... up -d --no-deps --no-recreate --scale worker-<lang>=N worker-<lang>`, which
starts or stops copies of that service only. Workers advertise their own address (--host auto) and pick
their own id, so copies need nothing else; the leader broker balances work across them by load.

On a machine whose shell has an old GEMINI_API_KEY that should not reach the containers, add --clean-env so
docker compose takes the key from .env instead.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field

LANGS = ("en", "my", "id", "ms", "th", "vi", "tl", "km", "lo")
SAMPLE = re.compile(r'^(polyglot_workers|polyglot_queue_depth|polyglot_waiting_jobs)\{lang="([a-z]+)"\} ([0-9.e+]+)$')


@dataclass
class Policy:
    minimum: int = 1
    maximum: int = 4
    high: float = 3.0          # backlog per worker that calls for another worker
    cooldown: float = 60.0     # seconds of calm before shrinking, and between changes of one pool


@dataclass
class PoolState:
    busy_readings: int = 0
    calm_since: float | None = None
    last_change: float = float("-inf")


@dataclass
class Autoscaler:
    policy: Policy
    pools: dict[str, PoolState] = field(default_factory=dict)

    def decide(self, observed: dict[str, dict[str, float]], now: float) -> dict[str, int]:
        """observed[lang] = {"workers": n, "queue": n, "waiting": n}. Returns {lang: new worker count} for the
        pools that should change now."""
        changes: dict[str, int] = {}
        for lang, numbers in observed.items():
            state = self.pools.setdefault(lang, PoolState())
            workers = int(numbers.get("workers", 0))
            backlog = float(numbers.get("queue", 0)) + float(numbers.get("waiting", 0))
            per_worker = backlog / workers if workers else (math.inf if backlog else 0.0)
            state.busy_readings = state.busy_readings + 1 if per_worker > self.policy.high else 0
            if backlog > 0:
                state.calm_since = None
            elif state.calm_since is None:
                state.calm_since = now
            recently_changed = now - state.last_change < self.policy.cooldown
            target = workers
            if workers < self.policy.minimum:
                target = self.policy.minimum
            elif (workers == 0 and backlog > 0) or (state.busy_readings >= 2 and not recently_changed):
                target = min(self.policy.maximum, workers + 1)
            elif (workers > self.policy.minimum and state.calm_since is not None
                  and now - state.calm_since >= self.policy.cooldown and not recently_changed):
                target = workers - 1
            if target != workers:
                changes[lang] = target
                state.last_change = now
                state.busy_readings = 0
        return changes


def parse_metrics(text: str) -> dict[str, dict[str, float]]:
    observed: dict[str, dict[str, float]] = {}
    names = {"polyglot_workers": "workers", "polyglot_queue_depth": "queue", "polyglot_waiting_jobs": "waiting"}
    for line in text.splitlines():
        match = SAMPLE.match(line.strip())
        if match:
            observed.setdefault(match.group(2), {})[names[match.group(1)]] = float(match.group(3))
    return observed


def read_metrics(url: str, token: str = "") -> dict[str, dict[str, float]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
        return parse_metrics(response.read().decode("utf-8"))


def service_for(lang: str) -> str:
    # English runs as two named services in the Compose files; the first one is the one that scales.
    return "worker-en-1" if lang == "en" else f"worker-{lang}"


def scale_command(compose_files: list[str], lang: str, count: int) -> list[str]:
    command = ["docker", "compose"]
    for path in compose_files:
        command += ["-f", path]
    service = service_for(lang)
    return command + ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{service}={count}", service]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000/metrics")
    parser.add_argument("--token", default="", help="the viewing token, if the dashboard has one")
    parser.add_argument("--compose", action="append", default=[], help="compose file (repeat, in order)")
    parser.add_argument("--min", type=int, default=1)
    parser.add_argument("--max", type=int, default=4)
    parser.add_argument("--high", type=float, default=3.0)
    parser.add_argument("--cooldown", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--langs", default=",".join(LANGS), help="which pools to manage")
    parser.add_argument("--once", action="store_true", help="one reading and decision, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print the decisions, change nothing")
    parser.add_argument("--clean-env", action="store_true", help="do not pass GEMINI_API_KEY/GOOGLE_API_KEY from this shell")
    args = parser.parse_args(argv)
    if args.min < 0 or args.max < max(1, args.min):
        parser.error("need 0 <= --min <= --max and --max >= 1")
    managed = {lang for lang in args.langs.split(",") if lang in LANGS}
    scaler = Autoscaler(Policy(args.min, args.max, args.high, args.cooldown))
    env = dict(os.environ)
    if args.clean_env:
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
    compose_files = args.compose or ["compose.yaml"]
    while True:
        try:
            observed = {lang: numbers for lang, numbers in read_metrics(args.url, args.token).items() if lang in managed}
        except OSError as exc:
            print(f"Could not read {args.url}: {exc}", flush=True)
            observed = {}
        for lang, count in sorted(scaler.decide(observed, time.monotonic()).items()):
            before = int(observed[lang].get("workers", 0))
            command = scale_command(compose_files, lang, count)
            print(f"{lang}: {before} -> {count} workers (queue {observed[lang].get('queue', 0):g}, "
                  f"waiting {observed[lang].get('waiting', 0):g})" + ("  [dry run]" if args.dry_run else ""), flush=True)
            if not args.dry_run:
                done = subprocess.run(command, env=env, capture_output=True, text=True)
                if done.returncode != 0:
                    print(f"  scaling failed: {done.stderr.strip()[-300:]}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
