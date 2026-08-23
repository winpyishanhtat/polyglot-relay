"""Compute WER, BLEU, and recovery-time summaries for Polyglot Relay."""
from __future__ import annotations
import argparse, collections, json, math, pathlib, re
from typing import Iterable

HERE = pathlib.Path(__file__).resolve().parent

def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_word in enumerate(reference, 1):
        current = [row]
        for col, hyp_word in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[col] + 1, previous[col - 1] + (ref_word != hyp_word)))
        previous = current
    return previous[-1]

def wer(pairs: Iterable[dict[str, str]]) -> float:
    errors = words = 0
    for pair in pairs:
        reference, hypothesis = pair["reference"].lower().split(), pair["hypothesis"].lower().split()
        errors += edit_distance(reference, hypothesis); words += len(reference)
    return errors / words if words else 0.0

def _tokens(text: str) -> list[str]:
    spaced = text.strip().split()
    return spaced if len(spaced) > 1 else list(text.strip())

def corpus_bleu(pairs: Iterable[dict[str, str]], max_order: int = 4) -> float:
    matches, totals = [0] * max_order, [0] * max_order
    ref_length = hyp_length = 0
    for pair in pairs:
        reference, hypothesis = _tokens(pair["reference"]), _tokens(pair["hypothesis"])
        ref_length += len(reference); hyp_length += len(hypothesis)
        for order in range(1, max_order + 1):
            ref = collections.Counter(tuple(reference[i:i + order]) for i in range(len(reference) - order + 1))
            hyp = collections.Counter(tuple(hypothesis[i:i + order]) for i in range(len(hypothesis) - order + 1))
            matches[order - 1] += sum((ref & hyp).values()); totals[order - 1] += sum(hyp.values())
    if not hyp_length: return 0.0
    precisions = [(matches[i] + 1) / (totals[i] + 1) for i in range(max_order)]
    brevity = 1.0 if hyp_length > ref_length else math.exp(1 - ref_length / hyp_length)
    return 100 * brevity * math.exp(sum(math.log(p) for p in precisions) / max_order)

def parse_timings(path: pathlib.Path | None) -> dict:
    if not path or not path.exists(): return {}
    events: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line); events[str(item["scenario"])][str(item["event"])] = float(item["timestamp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            match = re.search(r"scenario=(\S+) event=(\S+) timestamp=([\d.]+)", line)
            if match: events[match[1]][match[2]] = float(match[3])
    return {scenario: {"recovery_seconds": round(stamps["recovered"] - stamps["failure"], 3)}
            for scenario, stamps in events.items() if "failure" in stamps and "recovered" in stamps}

def evaluate(benchmark_path: pathlib.Path, scenario_log: pathlib.Path | None = None) -> dict:
    data = json.loads(benchmark_path.read_text(encoding="utf-8"))
    return {"wer": round(wer(data.get("transcription", [])), 6),
            "bleu": {lang: round(corpus_bleu(pairs), 3) for lang, pairs in data.get("translation", {}).items()},
            "failure_recovery": parse_timings(scenario_log)}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=pathlib.Path, default=HERE / "benchmark_sentences.json")
    parser.add_argument("--scenario-log", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, default=HERE / "results.json")
    args = parser.parse_args(); results = evaluate(args.benchmark, args.scenario_log)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
