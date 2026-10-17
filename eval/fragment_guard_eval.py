"""Measures the spurious-fragment guard (fragment_guard.py) on FLEURS before it is trusted with live speech.

    python -m eval.fragment_guard_eval --data D:/UIT/fleurs_data/run150 --out results/fragment_guard.json

Two questions, answered separately:

1. Does it ever drop real speech? Every FLEURS reference sentence, and every transcript Gemini and Whisper
   actually produced for FLEURS audio, is checked twice: as if the audio were loud, and as if it were quiet
   (the strictest case). Any sentence it would drop is listed.
2. Does it catch what it is meant to? A list of fragments that transcribers produce for silence and music
   (from our own logs and widely reported Whisper behaviour) is checked, each with quiet audio.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fragment_guard  # noqa: E402

KNOWN_FRAGMENTS = [
    "Thank you.", "Thanks for watching!", "Thank you for watching.", " you", "Bye.", "...", "♪", "♪♪♪", "[Music]",
    "(upbeat music)", "Subtitles by the Amara.org community", "Please subscribe to my channel.",
    "Hãy subscribe cho kênh Ghiền Mì Gõ Để không bỏ lỡ những video hấp dẫn", "Cảm ơn các bạn đã theo dõi.",
    "Terima kasih telah menonton.", "ご視聴ありがとうございました", "the the the the the the the",
    "ขอบคุณครับ", "ขอบคุณที่รับชม", "Okay.", "so so so so so so", "ააააააააააააა",
]


def load_sentences(data: pathlib.Path) -> list[dict]:
    sentences = []
    with open(data / "manifest.jsonl", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            sentences.append({"source": "reference", "lang": row["lang"], "id": row["id"], "text": row["transcript"]})
    for path in sorted((data / "results").glob("asr_*.json")):
        if "before_guard_fix" in path.name:
            continue
        system = "gemini" if "gemini" in path.name else "whisper-or-routed"
        for row in json.loads(path.read_text(encoding="utf-8")).get("results", []):
            if row.get("hypothesis"):
                sentences.append({"source": f"transcript:{system}", "lang": row["lang"], "id": row["id"], "text": row["hypothesis"]})
    return sentences


def evaluate(data: pathlib.Path) -> dict:
    sentences = load_sentences(data)
    dropped_loud = [s for s in sentences if fragment_guard.reason(s["text"], rms=1.0)]
    dropped_quiet = [s for s in sentences if fragment_guard.reason(s["text"], rms=0.0)]
    by_lang: dict[str, int] = {}
    for s in sentences:
        by_lang[s["lang"]] = by_lang.get(s["lang"], 0) + 1
    caught = {text: fragment_guard.reason(text, rms=0.0) for text in KNOWN_FRAGMENTS}
    return {
        "sentences_checked": len(sentences),
        "by_language": by_lang,
        "by_source": {source: sum(1 for s in sentences if s["source"] == source) for source in sorted({s["source"] for s in sentences})},
        "real_speech_dropped_if_loud": len(dropped_loud),
        "real_speech_dropped_if_quiet": len(dropped_quiet),
        "dropped_examples": [{**s, "reason": fragment_guard.reason(s["text"], rms=0.0)} for s in dropped_quiet[:20]],
        "known_fragments": len(KNOWN_FRAGMENTS),
        "known_fragments_caught": sum(1 for r in caught.values() if r),
        "missed_fragments": [text for text, r in caught.items() if not r],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="D:/UIT/fleurs_data/run150")
    parser.add_argument("--out", default=str(ROOT / "results" / "fragment_guard.json"))
    args = parser.parse_args(argv)
    report = evaluate(pathlib.Path(args.data))
    pathlib.Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Checked {report['sentences_checked']} real sentences: dropped {report['real_speech_dropped_if_loud']} "
          f"(loud audio), {report['real_speech_dropped_if_quiet']} (quiet audio). Caught "
          f"{report['known_fragments_caught']} of {report['known_fragments']} known fragments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
