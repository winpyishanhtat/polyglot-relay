"""Build an evaluation manifest from FLEURS (google/fleurs, CC-BY-4.0).

For each language this streams the start of the test-split archive from Hugging Face, keeps the
first N recordings (the connection is closed as soon as N are extracted, so only a fraction of the
archive is downloaded), and writes manifest.jsonl for eval.fleurs_eval. The English reference for
each sentence is the English recording's transcript with the same FLEURS sentence id.

    python -m eval.prepare_fleurs --out D:/data/fleurs --clips 150

Each language gets the first N clips *in its own archive*, so different languages are scored on
different sentences. Difficulty differs between sentences, so compare languages with care. An
exact like-for-like comparison needs the full archives (250-650 MB per language), because the
clips for a given sentence sit at unrelated places in each archive.

Downloaded files are untrusted data: only plain ".wav" members with digit names under a size
limit are written, and always to the output directory under a name this tool chooses.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import pathlib
import re
import tarfile
import time
import urllib.error
import urllib.request

BASE_URL = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"
# FLEURS code -> this project's language code
LANGUAGES = {"en_us": "en", "my_mm": "my", "id_id": "id", "ms_my": "ms", "th_th": "th",
             "vi_vn": "vi", "fil_ph": "tl", "km_kh": "km", "lo_la": "lo"}
MAX_WAV_BYTES = 12 * 1024 * 1024
_MEMBER = re.compile(r"^(?:test/)?(\d{1,24})\.wav$")


def fetch(url: str, timeout: float = 60.0):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "polyglot-relay-eval"}), timeout=timeout)


def read_tsv(text: str) -> list[list[str]]:
    """FLEURS test.tsv: id, file name, raw transcript, normalised transcript, characters, samples, gender."""
    rows = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter="\t", quoting=csv.QUOTE_NONE) if len(r) >= 4]
    return rows


def extract_first(stream, wanted: dict[str, list[str]], destination: pathlib.Path, count: int,
                  already: frozenset[str] = frozenset()) -> tuple[list[str], bool]:
    """Stream a .tar.gz and keep the first `count` members (not already kept) whose file name is in
    `wanted`. Returns (kept names, whether the stream ended without a network error). Stops reading,
    and so downloading, as soon as enough are kept."""
    destination.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                match = _MEMBER.match(member.name) if member.isfile() else None
                if not match or member.size > MAX_WAV_BYTES:
                    continue
                name = f"{match.group(1)}.wav"
                if name not in wanted or name in kept or name in already:
                    continue
                data = archive.extractfile(member).read()
                (destination / name).write_bytes(data)
                kept.append(name)
                if len(kept) + len(already) >= count:
                    break
    except (tarfile.TarError, EOFError, OSError):
        return kept, False  # dropped mid-archive: keep what arrived whole, let the caller retry
    return kept, True


def with_retries(action, attempts: int = 3, pause: float = 2.0):
    """Run action(), retrying on network errors (a timeout once ended a half-hour download)."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (OSError, TimeoutError) as exc:
            permanent = isinstance(exc, urllib.error.HTTPError) and exc.code < 500
            if attempt == attempts or permanent:
                raise
            time.sleep(pause * attempt)


def prepare(out: pathlib.Path, clips: int, languages: dict[str, str] | None = None,
            base_url: str = BASE_URL, opener=fetch) -> list[dict]:
    languages = languages or LANGUAGES
    tsvs: dict[str, list[list[str]]] = {}
    for code in languages:
        tsvs[code] = read_tsv(with_retries(lambda: opener(f"{base_url}/{code}/test.tsv").read()).decode("utf-8"))
    english = {}
    if "en_us" in tsvs:
        for row in tsvs["en_us"]:
            english.setdefault(row[0], row[2])
    else:
        for row in read_tsv(with_retries(lambda: opener(f"{base_url}/en_us/test.tsv").read()).decode("utf-8")):
            english.setdefault(row[0], row[2])

    manifest: list[dict] = []
    for code, lang in languages.items():
        by_file = {row[1]: row for row in tsvs[code]}
        directory = out / "audio" / lang
        kept: list[str] = []
        for attempt in range(1, 4):   # a dropped connection resumes with the clips still missing
            try:
                with opener(f"{base_url}/{code}/audio/test.tar.gz") as stream:
                    more, clean = extract_first(stream, by_file, directory, clips, frozenset(kept))
            except urllib.error.HTTPError as exc:
                if exc.code < 500:   # missing or forbidden: retrying cannot help
                    raise
                more, clean = [], False
            except OSError:
                more, clean = [], False
            kept += more
            if clean or len(kept) >= clips:
                break
            time.sleep(2.0 * attempt)
        if not kept:
            raise RuntimeError(f"{lang}: no clips could be downloaded from {base_url}/{code}/audio/test.tar.gz")
        for name in kept:
            row = by_file[name]
            entry = {"id": f"{lang}-{name[:-4]}", "lang": lang, "audio": f"audio/{lang}/{name}",
                     "transcript": row[2], "sentence_id": row[0]}
            if lang != "en" and row[0] in english:
                entry["english"] = english[row[0]]
            manifest.append(entry)
        print(f"{lang}: kept {len(kept)} clips", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in manifest) + "\n",
                                        encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=pathlib.Path, required=True, help="new directory for audio and manifest.jsonl")
    parser.add_argument("--clips", type=int, default=100, help="recordings to keep per language")
    parser.add_argument("--langs", nargs="*", help="project language codes to include (default: all nine)")
    args = parser.parse_args(argv)
    chosen = {c: l for c, l in LANGUAGES.items() if not args.langs or l in args.langs}
    manifest = prepare(args.out, args.clips, chosen)
    print(f"manifest: {len(manifest)} clips -> {args.out / 'manifest.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
