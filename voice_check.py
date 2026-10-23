"""Is there a voice in this audio at all? A check on the sound itself, before anything is transcribed.

Speech recognisers invent words for sounds that are not speech: on the live stack Gemini turned faint hiss into
"Is that better?" and a 60 Hz hum into "subsonic". A window is rejected only when it is steady - its loudness
varies by less than MAX_STEADY_DB between 32 ms frames, while speech rises and falls with every syllable - and,
in addition, one of these says it is not a voice:

* voice share - the fraction of the energy between 300 and 3400 Hz, where speech carries its information. Hum,
  rumble and traffic put almost everything below 300 Hz.
* spectral flatness - how evenly the energy spreads over 100-4000 Hz (1 = perfectly flat). Hiss, fans and
  white noise are flat; speech has formants and harmonics, so it is far from flat.

The thresholds were set from measurements, not guessed (eval/voice_check_eval.py, results/voice_check.json):
see that file for how many real FLEURS recordings and 3-second stretches of them pass, and which kinds of noise
are rejected. POLYGLOT_VOICE_CHECK=0 turns it off. Needs numpy (present wherever audio is transcribed).
"""
from __future__ import annotations

import os
from typing import Any

MAX_STEADY_DB = 6.0         # speech rises and falls with every syllable; a sound steadier than this is a candidate
MIN_VOICE_SHARE = 0.15      # ...and then rejected if the energy is not where a voice puts it (hum, rumble)
MAX_FLATNESS = 0.50         # ...or if the spectrum is as flat as noise (hiss, fan)


def enabled() -> bool:
    return (os.getenv("POLYGLOT_VOICE_CHECK") or "1") != "0"


def profile(window: Any, sample_rate: int) -> tuple[float, float, float] | None:
    """(voice share, spectral flatness, loudness variation in dB) of a mono window, or None when it cannot be
    measured. The variation is the 90th over the 10th percentile of the energy of 32 ms frames."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        x = np.asarray(window, dtype="float64").ravel()
    except (TypeError, ValueError):
        return None
    frame = 1024
    if sample_rate <= 0 or x.size < frame * 2:
        return None
    hop = frame // 2
    count = 1 + (x.size - frame) // hop
    frames = np.lib.stride_tricks.as_strided(x, shape=(count, frame), strides=(x.strides[0] * hop, x.strides[0]))
    spectrum = (np.abs(np.fft.rfft(frames * np.hanning(frame), axis=1)) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
    total = spectrum[freqs >= 50].sum()
    if total <= 0:
        return None
    voice = spectrum[(freqs >= 300) & (freqs <= 3400)].sum() / total
    band = spectrum[(freqs >= 100) & (freqs <= 4000)] + 1e-20
    flatness = float(np.exp(np.mean(np.log(band))) / np.mean(band))
    short = max(1, int(0.032 * sample_rate))
    usable = x[: x.size - x.size % short].reshape(-1, short)
    energy = (usable ** 2).mean(axis=1) + 1e-12
    variation = float(10 * np.log10(np.percentile(energy, 90) / np.percentile(energy, 10)))
    return float(voice), flatness, variation


def reason(window: Any, sample_rate: int) -> str | None:
    """Why this window holds no voice, or None to transcribe it (also when it cannot be measured)."""
    measured = profile(window, sample_rate)
    if measured is None:
        return None
    voice, flatness, variation = measured
    if variation >= MAX_STEADY_DB:
        return None                  # it rises and falls like speech
    if voice < MIN_VOICE_SHARE:
        return f"almost no energy in the voice band ({voice:.0%}): hum or rumble"
    if flatness > MAX_FLATNESS:
        return f"a flat, noise-like spectrum (flatness {flatness:.2f}): hiss or fan"
    return None


def not_a_voice(window: Any, sample_rate: int) -> bool:
    return enabled() and reason(window, sample_rate) is not None
