"""Measure speech recognition and translation quality on real recordings.

The input is a manifest: one JSON object per line.

    {"id": "my-0001", "lang": "my", "audio": "my/0001.wav",
     "transcript": "<what was said, in the original script>",
     "english": "<a human English translation of it>"}        # "english" is optional

`audio` is resolved relative to the manifest. FLEURS (google/fleurs) has this shape: it holds
recordings and transcripts for Burmese, Khmer, Lao and the other ASEAN languages here, and the
same sentences are aligned across languages, so the English reference can be taken from the
English split. Your own consented recordings work the same way.

    python -m eval.fleurs_eval asr --manifest data/manifest.jsonl --out results/asr.json
    python -m eval.fleurs_eval mt  --manifest data/manifest.jsonl --out results/mt.json
    python -m eval.fleurs_eval mt  --manifest data/manifest.jsonl --from-asr results/asr.json ...

`asr` runs the relay's real speech-to-text path (Gemini for Burmese/Khmer/Lao, Whisper for the
rest) and reports character and word error rate, an empty-output count, time per clip and
real-time factor, each with a 95% bootstrap interval. `mt` translates into English with the
configured backend (set TRANSLATION_BACKEND and the Gemini variables as for the relay) and
reports chrF++ against the human translation, either from the reference transcript (isolates
translation) or from the ASR output (end to end).

Failures count against the system: a clip that produced no text scores 100% character error,
never "skipped".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import time
import wave
from collections.abc import Callable, Iterable

from eval import metrics

BOOTSTRAP_SAMPLES = 1000


def load_manifest(path: pathlib.Path, languages: Iterable[str] | None = None,
                  limit_per_language: int | None = None) -> list[dict]:
    """Read and validate a manifest; audio paths become absolute."""
    wanted = set(languages) if languages else None
    items: list[dict] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: not valid JSON ({exc})") from exc
        for field in ("id", "lang", "audio", "transcript"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"{path}:{number}: missing or empty {field!r}")
        if wanted and item["lang"] not in wanted:
            continue
        if limit_per_language is not None and seen.get(item["lang"], 0) >= limit_per_language:
            continue
        seen[item["lang"]] = seen.get(item["lang"], 0) + 1
        audio = pathlib.Path(item["audio"])
        item["audio"] = str(audio if audio.is_absolute() else (path.parent / audio).resolve())
        items.append(item)
    if not items:
        raise ValueError(f"{path}: no usable entries")
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate ids")
    return items


def wav_seconds(path: str) -> float | None:
    try:
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, FileNotFoundError):
        return None


def default_transcriber(model_name: str = "base") -> Callable[[str, str], tuple[str, int]]:
    """The relay's own speech-to-text: (text, windows Gemini could not transcribe)."""
    from stt_service import transcribe_file

    def transcribe(path: str, language: str) -> tuple[str, int]:
        gaps = [0]
        pieces = [text for text, _pcm, _lang in transcribe_file(
            pathlib.Path(path), model_name, language=language,
            on_unrecognized=lambda: gaps.__setitem__(0, gaps[0] + 1))]
        return " ".join(pieces), gaps[0]
    return transcribe


def run_asr(items: list[dict], transcribe: Callable[[str, str], tuple[str, int]],
            clock: Callable[[], float] = time.monotonic) -> list[dict]:
    results = []
    for item in items:
        started = clock()
        error = None
        try:
            text, gaps = transcribe(item["audio"], item["lang"])
        except Exception as exc:  # a crash is a failed clip, not a missing one
            text, gaps, error = "", 0, f"{type(exc).__name__}: {exc}"
        elapsed = clock() - started
        char_edits, char_total = metrics.cer_counts(item["transcript"], text)
        word_edits, word_total = metrics.wer_counts(item["transcript"], text)
        results.append({"id": item["id"], "lang": item["lang"], "hypothesis": text,
                        "reference": item["transcript"], "char": [char_edits, char_total],
                        "word": [word_edits, word_total], "seconds": round(elapsed, 3),
                        "audio_seconds": wav_seconds(item["audio"]), "gaps": gaps, "error": error})
    return results


def _interval(counts: list[tuple[int, int]]) -> list[float]:
    low, high = metrics.bootstrap_ci(counts, metrics.rate, BOOTSTRAP_SAMPLES)
    return [round(100 * low, 2), round(100 * high, 2)]


def summarize_asr(results: list[dict]) -> dict:
    by_lang: dict[str, list[dict]] = {}
    for r in results:
        by_lang.setdefault(r["lang"], []).append(r)
    summary = {}
    for lang, rows in sorted(by_lang.items()):
        chars = [tuple(r["char"]) for r in rows]
        words = [tuple(r["word"]) for r in rows]
        seconds = [r["seconds"] for r in rows]
        durations = [r["audio_seconds"] for r in rows if r["audio_seconds"]]
        timed = [r for r in rows if r["audio_seconds"]]
        summary[lang] = {
            "utterances": len(rows),
            "cer_percent": round(100 * metrics.rate(chars), 2), "cer_ci95": _interval(chars),
            "wer_percent": round(100 * metrics.rate(words), 2), "wer_ci95": _interval(words),
            "empty_outputs": sum(1 for r in rows if not r["hypothesis"].strip()),
            "errors": sum(1 for r in rows if r["error"]),
            "unrecognised_windows": sum(r["gaps"] for r in rows),
            "median_seconds": round(statistics.median(seconds), 2),
            "audio_seconds_total": round(sum(durations), 1),
            "real_time_factor": round(sum(r["seconds"] for r in timed) / sum(r["audio_seconds"] for r in timed), 3)
                                if timed else None,
        }
    return summary


def default_translator(target: str = "en") -> Callable[[str, str], str]:
    from translator_worker import Translator
    translator = Translator(target)

    def translate(text: str, source_lang: str) -> str:
        return translator.translate(text, source_lang=source_lang)[0]
    return translate


def run_mt(items: list[dict], translate: Callable[[str, str], str],
           hypotheses: dict[str, str] | None = None) -> list[dict]:
    """Translate into English. hypotheses (id -> ASR text) switches to end-to-end scoring;
    a clip with no ASR text is translated from the empty string, i.e. scores as a failure."""
    results = []
    for item in items:
        if not item.get("english"):
            continue
        source_text = item["transcript"] if hypotheses is None else hypotheses.get(item["id"], "")
        error = None
        try:
            output = translate(source_text, item["lang"]) if source_text.strip() else ""
        except Exception as exc:
            output, error = "", f"{type(exc).__name__}: {exc}"
        results.append({"id": item["id"], "lang": item["lang"], "source": source_text, "output": output,
                        "reference": item["english"], "stats": metrics.chrf_stats(item["english"], output),
                        "error": error})
    return results


def summarize_mt(results: list[dict]) -> dict:
    by_lang: dict[str, list[dict]] = {}
    for r in results:
        by_lang.setdefault(r["lang"], []).append(r)
    summary = {}
    for lang, rows in sorted(by_lang.items()):
        stats = [[tuple(t) for t in r["stats"]] for r in rows]
        low, high = metrics.bootstrap_ci(stats, metrics.chrf_from_stats, BOOTSTRAP_SAMPLES)
        summary[lang] = {"sentences": len(rows), "chrf_pp": round(metrics.chrf_from_stats(stats), 2),
                         "chrf_pp_ci95": [round(low, 2), round(high, 2)],
                         "empty_outputs": sum(1 for r in rows if not r["output"].strip()),
                         "errors": sum(1 for r in rows if r["error"])}
    return summary


def markdown_table(summary: dict, columns: list[tuple[str, str, str | None]]) -> str:
    """columns: (heading, key, interval key or None). An interval renders as 'value (low-high)'."""
    lines = ["| Language | " + " | ".join(c[0] for c in columns) + " |", "|---|" + "---|" * len(columns)]
    for lang, row in summary.items():
        cells = []
        for _, key, interval in columns:
            value = row.get(key)
            if value is None:
                cells.append("-")
            elif interval and row.get(interval):
                low, high = row[interval]
                cells.append(f"{value} ({low}-{high})")
            else:
                cells.append(str(value))
        lines.append(f"| {lang} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


ASR_COLUMNS = [("n", "utterances", None), ("CER %", "cer_percent", "cer_ci95"), ("WER %", "wer_percent", "wer_ci95"),
               ("empty", "empty_outputs", None), ("median s", "median_seconds", None), ("RTF", "real_time_factor", None)]
MT_COLUMNS = [("n", "sentences", None), ("chrF++", "chrf_pp", "chrf_pp_ci95"), ("empty", "empty_outputs", None),
              ("errors", "errors", None)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["asr", "mt"])
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True, help="where to write the JSON results")
    parser.add_argument("--langs", nargs="*", help="only these language codes")
    parser.add_argument("--limit", type=int, help="at most this many clips per language")
    parser.add_argument("--model", default="base", help="Whisper model for the languages Whisper reads")
    parser.add_argument("--from-asr", type=pathlib.Path, help="mt only: translate this asr run's output")
    args = parser.parse_args(argv)

    items = load_manifest(args.manifest, args.langs, args.limit)
    if args.mode == "asr":
        results = run_asr(items, default_transcriber(args.model))
        summary = summarize_asr(results)
        table = markdown_table(summary, ASR_COLUMNS)
    else:
        hypotheses = None
        if args.from_asr:
            hypotheses = {r["id"]: r["hypothesis"] for r in json.loads(args.from_asr.read_text(encoding="utf-8"))["results"]}
        results = run_mt(items, default_translator("en"), hypotheses)
        summary = summarize_mt(results)
        table = markdown_table(summary, MT_COLUMNS)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"mode": args.mode, "summary": summary, "results": results},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
