"""Measure how long the relay takes to elect a new leader after the leader is killed.

Each trial starts two real broker processes, waits for the higher id (2) to win the Bully
election, kills it with no warning, and times how long until broker 1 reports itself leader
(polled every 25 ms through the same heartbeat message the dashboard uses). Run enough trials
to report a distribution rather than one lucky number:

    python -m eval.failover_trials --trials 30 --out results/failover.json

The brokers use the deployed timing settings unless you override them (POLYGLOT_HEARTBEAT_*,
POLYGLOT_ELECTION_TIMEOUT, POLYGLOT_CONNECT_TIMEOUT), and the settings used are written next
to the results so the numbers can be reproduced.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import time

from common import config
from common.protocol import encode, recv_one
from eval import metrics

ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMING_VARIABLES = ("POLYGLOT_HEARTBEAT_INTERVAL", "POLYGLOT_HEARTBEAT_TIMEOUT",
                    "POLYGLOT_ELECTION_TIMEOUT", "POLYGLOT_CONNECT_TIMEOUT")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def heartbeat(port: int, timeout: float = 0.5) -> dict | None:
    """The broker's own answer to a heartbeat, or None if it does not answer."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(encode({"type": "heartbeat", "sender_id": "trial", "role": "broker"}))
            return recv_one(sock)
    except (OSError, ConnectionError, ValueError):
        return None


def wait_until(condition, timeout: float, interval: float = 0.025) -> float | None:
    """Seconds until condition() is true, or None on timeout."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if condition():
            return time.monotonic() - started
        time.sleep(interval)
    return None


def run_trial(env_overrides: dict[str, str] | None = None, settle_timeout: float = 30.0,
              recovery_timeout: float = 60.0) -> float:
    """One kill-the-leader trial; returns the seconds to a new leader. Raises on a failed setup
    or on no recovery, so a broken run is never averaged in."""
    ports = {1: free_port(), 2: free_port()}
    env = os.environ.copy()
    env.update(POLYGLOT_BROKER_1_HOST="127.0.0.1", POLYGLOT_BROKER_2_HOST="127.0.0.1",
               POLYGLOT_BROKER_1_PORT=str(ports[1]), POLYGLOT_BROKER_2_PORT=str(ports[2]))
    env.pop("POLYGLOT_BROKER_BIND_HOST", None)
    env.update(env_overrides or {})
    procs = {i: subprocess.Popen([sys.executable, "broker.py", "--id", str(i)], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in (1, 2)}
    try:
        def settled() -> bool:
            leader, follower = heartbeat(ports[2]), heartbeat(ports[1])
            return bool(leader and leader.get("is_leader") and follower and follower.get("leader_id") == 2)
        if wait_until(settled, settle_timeout) is None:
            raise RuntimeError("the brokers never agreed on broker 2 as leader")
        procs[2].kill()
        killed_at = time.monotonic()
        procs[2].wait(timeout=5)

        def recovered() -> bool:
            answer = heartbeat(ports[1])
            return bool(answer and answer.get("is_leader"))
        if wait_until(recovered, recovery_timeout) is None:
            raise RuntimeError("broker 1 never became leader after the leader was killed")
        return time.monotonic() - killed_at
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def summarize(times: list[float]) -> dict:
    if not times:
        return {"trials": 0}
    mean_counts = [(t,) for t in times]
    low, high = metrics.bootstrap_ci(mean_counts, lambda sample: statistics.fmean(c[0] for c in sample))
    ordered = sorted(times)
    return {"trials": len(times), "mean_seconds": round(statistics.fmean(times), 3),
            "median_seconds": round(statistics.median(times), 3), "min_seconds": round(ordered[0], 3),
            "max_seconds": round(ordered[-1], 3),
            "stdev_seconds": round(statistics.stdev(times), 3) if len(times) > 1 else 0.0,
            "p95_seconds": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
            "mean_ci95": [round(low, 3), round(high, 3)]}


def timing_settings(env_overrides: dict[str, str] | None = None) -> dict[str, float]:
    """The timing values the brokers will actually run with."""
    merged = {**os.environ, **(env_overrides or {})}
    defaults = {"POLYGLOT_HEARTBEAT_INTERVAL": config.HEARTBEAT_INTERVAL,
                "POLYGLOT_HEARTBEAT_TIMEOUT": config.HEARTBEAT_TIMEOUT,
                "POLYGLOT_ELECTION_TIMEOUT": config.ELECTION_TIMEOUT,
                "POLYGLOT_CONNECT_TIMEOUT": config.CONNECT_TIMEOUT}
    return {name: float(merged[name]) if merged.get(name) else defaults[name] for name in TIMING_VARIABLES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    times: list[float] = []
    for number in range(1, args.trials + 1):
        seconds = run_trial()
        times.append(seconds)
        print(f"trial {number}/{args.trials}: new leader after {seconds:.2f}s", flush=True)
    result = {"settings": timing_settings(), "recovery_seconds": [round(t, 3) for t in times],
              "summary": summarize(times)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
