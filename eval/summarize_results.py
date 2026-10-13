"""Merge the per-language result files from eval.fleurs_eval into one table.

    python -m eval.summarize_results --results D:/data/fleurs/results --out report.md

Reads asr_<lang>.json, mt_ref_<lang>.json (translation from the reference transcript) and
mt_e2e_<lang>.json (translation from the recognised text), whichever exist.
"""
from __future__ import annotations

import argparse
import json
import pathlib

LANGUAGE_ORDER = ["en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"]
# Scripts written without spaces between words: word error rate is not meaningful for them.
UNSPACED = {"my", "th", "km", "lo"}


def _load(results: pathlib.Path, prefix: str, lang: str) -> dict | None:
    path = results / f"{prefix}_{lang}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("summary", {}).get(lang)


def _ci(value, interval) -> str:
    return f"{value} ({interval[0]}-{interval[1]})" if interval else str(value)


def build_rows(results: pathlib.Path) -> list[dict]:
    rows = []
    for lang in LANGUAGE_ORDER:
        asr, ref, e2e = _load(results, "asr", lang), _load(results, "mt_ref", lang), _load(results, "mt_e2e", lang)
        if not (asr or ref or e2e):
            continue
        rows.append({"lang": lang, "asr": asr, "mt_ref": ref, "mt_e2e": e2e})
    return rows


def markdown(rows: list[dict]) -> str:
    lines = ["| Language | Clips | CER % (95% CI) | WER % | Empty | Median s | RTF | chrF++ from reference | chrF++ end to end |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        asr, ref, e2e = row["asr"], row["mt_ref"], row["mt_e2e"]
        cells = [row["lang"]]
        if asr:
            wer = "n/a" if row["lang"] in UNSPACED else _ci(asr["wer_percent"], asr.get("wer_ci95"))
            cells += [str(asr["utterances"]), _ci(asr["cer_percent"], asr.get("cer_ci95")), wer, str(asr["empty_outputs"]),
                      str(asr["median_seconds"]), str(asr["real_time_factor"] if asr["real_time_factor"] is not None else "-")]
        else:
            cells += ["-"] * 6
        for mt in (ref, e2e):
            cells.append(_ci(mt["chrf_pp"], mt.get("chrf_pp_ci95")) if mt else "-")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args(argv)
    table = markdown(build_rows(args.results))
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
