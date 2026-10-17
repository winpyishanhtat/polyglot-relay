"""Compare two FLEURS translation runs (e.g. without and with context, or without and with a glossary).

    python -m eval.compare_mt results/mt_plain.json results/mt_context.json

For each language it reports chrF++ of both runs on the sentences they share, the difference, and a 95%
interval for the difference from a paired bootstrap (the same sentences are resampled for both runs), so a
difference whose interval contains 0 is not evidence of anything.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import metrics  # noqa: E402


def _stats(row: dict) -> list[tuple[int, int, int]]:
    return [tuple(t) for t in row["stats"]]


def compare(before: dict, after: dict, samples: int = 1000, seed: int = 0) -> dict:
    a = {r["id"]: r for r in before["results"]}
    b = {r["id"]: r for r in after["results"]}
    shared = sorted(set(a) & set(b))
    by_lang: dict[str, list[str]] = {}
    for sentence_id in shared:
        by_lang.setdefault(a[sentence_id]["lang"], []).append(sentence_id)
    rng = random.Random(seed)
    report = {}
    for lang, ids in sorted(by_lang.items()):
        sa = [_stats(a[i]) for i in ids]
        sb = [_stats(b[i]) for i in ids]
        score_a, score_b = metrics.chrf_from_stats(sa), metrics.chrf_from_stats(sb)
        diffs = []
        for _ in range(samples):
            pick = [rng.randrange(len(ids)) for _ in ids]
            diffs.append(metrics.chrf_from_stats([sb[k] for k in pick]) - metrics.chrf_from_stats([sa[k] for k in pick]))
        diffs.sort()
        low, high = diffs[int(0.025 * samples)], diffs[int(0.975 * samples) - 1]
        report[lang] = {"sentences": len(ids), "before": round(score_a, 2), "after": round(score_b, 2),
                        "difference": round(score_b - score_a, 2), "difference_ci95": [round(low, 2), round(high, 2)],
                        "significant": low > 0 or high < 0}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", type=pathlib.Path)
    parser.add_argument("after", type=pathlib.Path)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    report = compare(json.loads(args.before.read_text(encoding="utf-8")), json.loads(args.after.read_text(encoding="utf-8")),
                     args.samples)
    print("| Language | Sentences | Before | After | Difference (95% CI) |\n|---|---|---|---|---|")
    for lang, row in report.items():
        mark = "" if row["significant"] else " (not significant)"
        print(f"| {lang} | {row['sentences']} | {row['before']} | {row['after']} | {row['difference']:+} "
              f"({row['difference_ci95'][0]:+} to {row['difference_ci95'][1]:+}){mark} |")
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
