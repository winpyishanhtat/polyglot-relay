"""Measures voice_check.py before it is trusted: real speech must never be rejected, and noise should be.

    python -m eval.voice_check_eval --data D:/UIT/fleurs_data/run150 --out results/voice_check.json

Real speech: every FLEURS recording in the manifest (nine languages). Not speech: synthetic white, pink and
brown noise at several levels, 50 and 60 Hz mains hum with harmonics, and the same mixed with a little hiss.
It reports the range of both measurements for each group and how many of each the current thresholds reject.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import voice_check  # noqa: E402

RATE = 16000


def read_wav(path: pathlib.Path) -> tuple[np.ndarray, int]:
    """Mono samples in -1..1 from a PCM (integer) or IEEE-float WAV - FLEURS ships 32-bit float."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a WAV file")
    position, fmt, samples = 12, None, None
    while position + 8 <= len(data):
        kind, size = data[position:position + 4], int.from_bytes(data[position + 4:position + 8], "little")
        body = data[position + 8:position + 8 + size]
        if kind == b"fmt ":
            fmt = (int.from_bytes(body[0:2], "little"), int.from_bytes(body[2:4], "little"),
                   int.from_bytes(body[4:8], "little"), int.from_bytes(body[14:16], "little"))
        elif kind == b"data":
            samples = body
        position += 8 + size + (size & 1)
    if fmt is None or samples is None:
        raise ValueError(f"{path} has no fmt or data chunk")
    tag, channels, rate, bits = fmt
    if tag == 0xFFFE:
        tag = 3 if bits in (32, 64) and len(samples) % (bits // 8) == 0 and bits != 16 else 1
    if tag == 3:
        audio = np.frombuffer(samples, dtype="<f4" if bits == 32 else "<f8").astype("float64")
    else:
        dtype = {8: np.uint8, 16: "<i2", 32: "<i4"}[bits]
        audio = np.frombuffer(samples[:len(samples) - len(samples) % (bits // 8)], dtype=dtype).astype("float64")
        audio = (audio - 128) / 128 if bits == 8 else audio / float(2 ** (bits - 1))
    if channels > 1:
        audio = audio[:len(audio) - len(audio) % channels].reshape(-1, channels).mean(axis=1)
    return audio, rate


def noises(seconds: float = 3.0, seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    pink = np.fft.irfft(spectrum / np.sqrt(np.maximum(freqs, 1)), n)
    brown = np.cumsum(white)
    brown -= np.linspace(brown[0], brown[-1], n)
    out = {}
    for level in (0.002, 0.02, 0.2):
        out[f"white {level}"] = level * white / np.abs(white).max()
        out[f"pink {level}"] = level * pink / np.abs(pink).max()
        out[f"brown {level}"] = level * brown / np.abs(brown).max()
    for mains in (50, 60):
        hum = sum(np.sin(2 * np.pi * mains * k * t) / k for k in (1, 2, 3))
        out[f"hum {mains} Hz"] = 0.3 * hum / np.abs(hum).max()
        out[f"hum {mains} Hz + hiss"] = 0.3 * hum / np.abs(hum).max() + 0.003 * white
    return out


def evaluate(data: pathlib.Path) -> dict:
    speech = []
    windows = []
    with open(data / "manifest.jsonl", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            audio, rate = read_wav(data / item["audio"])
            measured = voice_check.profile(audio, rate)
            if measured:
                speech.append({"id": item["id"], "lang": item["lang"], "voice": measured[0], "flatness": measured[1],
                               "variation_db": measured[2], "rejected": voice_check.reason(audio, rate)})
            for start in range(0, len(audio) - 3 * rate + 1, 3 * rate):      # live slices are 2-8 s: check 3 s pieces
                piece = audio[start:start + 3 * rate]
                windows.append({"id": item["id"], "lang": item["lang"], "start_s": start // rate,
                                "rejected": voice_check.reason(piece, rate)})
    synthetic = []
    for name, audio in noises().items():
        voice, flatness, variation = voice_check.profile(audio, RATE)
        synthetic.append({"name": name, "voice": round(voice, 4), "flatness": round(flatness, 4),
                          "variation_db": round(variation, 1), "rejected": voice_check.reason(audio, RATE)})
    return {
        "thresholds": {"max_steady_db": voice_check.MAX_STEADY_DB, "min_voice_share": voice_check.MIN_VOICE_SHARE,
                       "max_flatness": voice_check.MAX_FLATNESS},
        "speech_windows_3s": len(windows),
        "speech_windows_rejected": sum(1 for w in windows if w["rejected"]),
        "speech_windows_rejected_examples": [w for w in windows if w["rejected"]][:10],
        "speech_recordings": len(speech),
        "speech_rejected": sum(1 for s in speech if s["rejected"]),
        "speech_voice_share": {"min": round(min(s["voice"] for s in speech), 4), "median": round(float(np.median([s["voice"] for s in speech])), 4)},
        "speech_flatness": {"max": round(max(s["flatness"] for s in speech), 4), "median": round(float(np.median([s["flatness"] for s in speech])), 4)},
        "speech_rejected_examples": [s for s in speech if s["rejected"]][:10],
        "noise": synthetic,
        "noise_rejected": sum(1 for s in synthetic if s["rejected"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="D:/UIT/fleurs_data/run150")
    parser.add_argument("--out", default=str(ROOT / "results" / "voice_check.json"))
    args = parser.parse_args(argv)
    report = evaluate(pathlib.Path(args.data))
    pathlib.Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"3-second pieces of speech: {report['speech_windows_rejected']} of {report['speech_windows_3s']} rejected.")
    print(f"Speech: {report['speech_rejected']} of {report['speech_recordings']} rejected; voice share min "
          f"{report['speech_voice_share']['min']}, flatness max {report['speech_flatness']['max']}.")
    print(f"Noise: {report['noise_rejected']} of {len(report['noise'])} rejected.")
    for row in report["noise"]:
        print(f"   {row['name']:<22} voice {row['voice']:.3f}  flatness {row['flatness']:.3f}  "
              f"variation {row['variation_db']:.1f} dB  {'rejected' if row['rejected'] else 'PASSED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
