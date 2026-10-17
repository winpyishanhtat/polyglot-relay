"""Prometheus metrics, without a client library: counters and histograms kept in memory and rendered in the
Prometheus text format at GET /metrics (see compose.monitoring.yaml for Prometheus and Grafana)."""
from __future__ import annotations

import threading
from collections import defaultdict

# Seconds from a sentence being said (recorded by the dashboard) to its translation arriving.
DELAY_BUCKETS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 60.0)


def _labels(pairs: dict[str, str]) -> str:
    if not pairs:
        return ""
    escaped = (f'{key}="{str(value).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34)).replace(chr(10), " ")}"'
               for key, value in sorted(pairs.items()))
    return "{" + ",".join(escaped) + "}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self._histograms: dict[str, dict[tuple, list]] = defaultdict(dict)
        self._help: dict[str, str] = {}

    def describe(self, name: str, text: str) -> None:
        self._help[name] = text

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counters[name][key] += amount

    def observe(self, name: str, value: float, buckets=DELAY_BUCKETS, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            entry = self._histograms[name].get(key)
            if entry is None:
                entry = self._histograms[name][key] = [list(buckets), [0] * len(buckets), 0.0, 0]
            for index, bound in enumerate(entry[0]):
                if value <= bound:
                    entry[1][index] += 1
            entry[2] += value
            entry[3] += 1

    def value(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)

    def render(self) -> list[str]:
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} counter")
                for key, value in sorted(series.items()):
                    lines.append(f"{name}{_labels(dict(key))} {value:g}")
            for name, series in sorted(self._histograms.items()):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} histogram")
                for key, (bounds, counts, total, count) in sorted(series.items()):
                    labels = dict(key)
                    for bound, bucket in zip(bounds, counts):
                        lines.append(f"{name}_bucket{_labels({**labels, 'le': f'{bound:g}'})} {bucket}")
                    lines.append(f"{name}_bucket{_labels({**labels, 'le': '+Inf'})} {count}")
                    lines.append(f"{name}_sum{_labels(labels)} {total:g}")
                    lines.append(f"{name}_count{_labels(labels)} {count}")
        return lines


def gauge(lines: list[str], name: str, help_text: str, samples: list[tuple[dict[str, str], float]]) -> None:
    """Append one gauge family (values computed at scrape time) to `lines`."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in samples:
        lines.append(f"{name}{_labels(labels)} {value:g}")


def counter_family(lines: list[str], name: str, help_text: str, samples: list[tuple[dict[str, str], float]]) -> None:
    """Append a counter family whose values another process keeps (e.g. the leader broker's job counters)."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    for labels, value in samples:
        lines.append(f"{name}{_labels(labels)} {value:g}")


REGISTRY = Registry()
REGISTRY.describe("polyglot_caption_delay_seconds", "Seconds from a sentence being recorded to its translation arriving, per language")
REGISTRY.describe("polyglot_captions_total", "Translated captions received by the dashboard, per room, language and backend")
REGISTRY.describe("polyglot_chunks_total", "Sentences sent for translation, per room")
REGISTRY.describe("polyglot_http_requests_total", "Dashboard HTTP requests, per route and status")
