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


def run_chain(env_overrides: dict[str, str] | None = None, brokers: int = 2, kills: int = 1,
              settle_timeout: float = 30.0, recovery_timeout: float = 60.0) -> list[float]:
    """Start `brokers` real brokers, then kill the leader `kills` times in a row, timing each recovery
    (seconds until the next-highest id reports itself leader). Raises on a failed setup or on no
    recovery, so a broken run is never averaged in."""
    if not 1 <= kills < brokers:
        raise ValueError("kills must be at least 1 and fewer than the number of brokers")
    ports = {number: free_port() for number in range(1, brokers + 1)}
    env = os.environ.copy()
    env["POLYGLOT_BROKER_COUNT"] = str(brokers)
    for number, port in ports.items():
        env[f"POLYGLOT_BROKER_{number}_HOST"] = "127.0.0.1"
        env[f"POLYGLOT_BROKER_{number}_PORT"] = str(port)
    env.pop("POLYGLOT_BROKER_BIND_HOST", None)
    env.update(env_overrides or {})
    procs = {number: subprocess.Popen([sys.executable, "broker.py", "--id", str(number)], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for number in ports}
    try:
        def settled() -> bool:
            top = heartbeat(ports[brokers])
            if not (top and top.get("is_leader")):
                return False
            return all((heartbeat(ports[n]) or {}).get("leader_id") == brokers for n in range(1, brokers))
        if wait_until(settled, settle_timeout) is None:
            raise RuntimeError(f"the brokers never agreed on broker {brokers} as leader")
        times = []
        leader = brokers
        for _ in range(kills):
            procs[leader].kill()
            killed_at = time.monotonic()
            procs[leader].wait(timeout=5)
            successor = leader - 1

            def recovered(successor=successor) -> bool:
                answer = heartbeat(ports[successor])
                return bool(answer and answer.get("is_leader"))
            if wait_until(recovered, recovery_timeout) is None:
                raise RuntimeError(f"broker {successor} never became leader after broker {leader} was killed")
            times.append(time.monotonic() - killed_at)
            leader = successor
            if _ != kills - 1:   # let the survivors agree before the next kill
                if wait_until(lambda: all((heartbeat(ports[n]) or {}).get("leader_id") == leader for n in range(1, leader)),
                              settle_timeout) is None:
                    raise RuntimeError("the surviving brokers never agreed on the new leader")
        return times
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def run_trial(env_overrides: dict[str, str] | None = None, settle_timeout: float = 30.0,
              recovery_timeout: float = 60.0) -> float:
    """One kill-the-leader trial with two brokers; returns the seconds to a new leader."""
    return run_chain(env_overrides, 2, 1, settle_timeout, recovery_timeout)[0]


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
    parser.add_argument("--brokers", type=int, default=2, help="how many broker replicas to start")
    parser.add_argument("--kills", type=int, default=1, help="how many leaders to kill in a row per trial")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    times: list[float] = []
    for number in range(1, args.trials + 1):
        chain = run_chain(None, args.brokers, args.kills)
        times.extend(chain)
        print(f"trial {number}/{args.trials}: new leader after " + ", then ".join(f"{t:.2f}s" for t in chain), flush=True)
    result = {"settings": timing_settings(), "brokers": args.brokers, "kills_per_trial": args.kills, "recovery_seconds": [round(t, 3) for t in times],
              "summary": summarize(times)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
