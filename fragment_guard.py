"""Drops transcripts that are not speech: what a transcriber invents for silence, noise or music.

Whisper (and occasionally Gemini) fills a quiet slice with stock phrases learned from subtitled videos -
"Thanks for watching!", "Subtitles by the Amara.org community", Vietnamese "Hãy subscribe cho kênh Ghiền Mì
Gõ…" - or loops on one word. Sent on, each such fragment becomes nine wrong captions. The rules are kept
narrow on purpose, because dropping a real sentence is worse than letting a fake one through:

* A transcript that is *entirely* one of the stock phrases below is dropped, whatever the audio.
* A transcript that is entirely a short courtesy ("Thank you", "Bye", "Okay") is dropped only when the
  audio was quiet, since people do say these.
* A transcript of nothing but symbols, or one word or syllable repeated over and over, is dropped.

Measured on FLEURS (eval/fragment_guard_eval.py, results/fragment_guard.json): no reference sentence and no
real transcript in any of the nine languages is dropped. POLYGLOT_FRAGMENT_GUARD=0 turns it off.
"""
from __future__ import annotations

import os
import re
import unicodedata

# Whole-transcript stock phrases (compared after normalise()).
ALWAYS = {
    "thanks for watching", "thank you for watching", "thank you so much for watching", "thanks for watching bye",
    "thank you for watching see you next time", "please subscribe", "please like and subscribe",
    "like and subscribe", "subscribe to my channel", "dont forget to like and subscribe",
    "please subscribe to my channel", "subtitles by the amaraorg community", "subtitles by amaraorg",
    "subtitles by", "transcribed by", "translated by", "captions by", "music", "background music", "applause",
    "laughter", "silence", "inaudible", "foreign", "music playing", "upbeat music", "soft music",
    # Vietnamese
    "hãy subscribe cho kênh ghiền mì gõ để không bỏ lỡ những video hấp dẫn",
    "hãy đăng ký kênh để ủng hộ kênh của mình nhé", "cảm ơn các bạn đã theo dõi", "cảm ơn các bạn đã xem video",
    "cảm ơn các bạn đã theo dõi và hẹn gặp lại", "hẹn gặp lại các bạn trong những video tiếp theo",
    # Indonesian / Malay
    "terima kasih telah menonton", "terima kasih sudah menonton", "terima kasih kerana menonton",
    "jangan lupa subscribe", "jangan lupa like dan subscribe",
    # Filipino
    "salamat sa panonood",
    # Thai
    "ขอบคุณที่รับชม", "ฝากกดติดตามด้วยนะครับ", "ฝากกดไลค์กดแชร์กดติดตามด้วยนะครับ",
    # languages a mis-detected slice often comes out in
    "ご視聴ありがとうございました", "请不吝点赞订阅转发打赏支持明镜与点点栏目", "字幕由amaraorg社区提供",
    "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다",
}

# Real when spoken; invented when the audio was quiet.
QUIET_ONLY = {
    "thank you", "thanks", "thank you very much", "you", "bye", "bye bye", "goodbye", "okay", "ok", "so",
    "the end", "uh", "um", "hmm", "mm", "oh", "yeah", "cảm ơn", "cảm ơn các bạn", "terima kasih", "salamat",
    "ขอบคุณ", "ขอบคุณครับ", "ขอบคุณค่ะ", "ok bye",
}

QUIET_RMS = 0.01          # a slice whose loudness (RMS, 0..1) stays under this is quiet

# The script each spoken language is written in. A transcript for one of these that is largely in ANOTHER
# non-Latin script is invented (seen live: a Khmer slice transcribed partly in Thai letters). Latin letters are
# always allowed: names, English words and numbers mix into every language.
_THAI, _LAO = (0x0E00, 0x0E7F), (0x0E80, 0x0EFF)
# Lao speech is often transcribed in Thai letters (the languages are close and the text still means the right
# thing; four such FLEURS transcripts were kept for that reason), so each of the two accepts the other's script.
SCRIPTS = {"my": [(0x1000, 0x109F)], "km": [(0x1780, 0x17FF)], "lo": [_LAO, _THAI], "th": [_THAI, _LAO]}
FOREIGN_SCRIPT_SHARE = 0.25


def foreign_script_share(text: str, lang: str | None) -> float:
    """Share of the letters that belong neither to `lang`'s script nor to Latin (0 when unknown)."""
    expected = SCRIPTS.get(lang or "")
    letters = [ch for ch in str(text) if unicodedata.category(ch)[0] in ("L", "M")]
    if not letters:
        return 0.0
    def foreign(ch: str) -> bool:
        code = ord(ch)
        if code < 0x0250 or 0x1E00 <= code <= 0x1EFF:          # Latin, including Vietnamese letters
            return False
        if expected is None:
            return False if lang in (None, "", "auto") else True   # a Latin-script language: any other script is foreign
        return not any(low <= code <= high for low, high in expected)
    return sum(1 for ch in letters if foreign(ch)) / len(letters)

_REPEAT = re.compile(r"(.{1,20}?)\1{4,}", re.DOTALL)


def enabled() -> bool:
    return (os.getenv("POLYGLOT_FRAGMENT_GUARD") or "1") != "0"


def normalise(text: str) -> str:
    """Lower case, punctuation and symbols removed (letters, combining marks and digits stay), single spaces."""
    kept = []
    for ch in unicodedata.normalize("NFC", str(text)).casefold():
        category = unicodedata.category(ch)
        if category[0] in ("L", "M", "N"):
            kept.append(ch)
        elif ch.isspace() or category[0] in ("P", "S", "Z"):
            kept.append(" ")
    return " ".join("".join(kept).split())


def reason(text: str, rms: float | None = None, lang: str | None = None) -> str | None:
    """Why `text` is not speech, or None when it should be kept. `rms` is the slice's loudness and `lang` the
    spoken language, when known."""
    if lang and foreign_script_share(text, lang) >= FOREIGN_SCRIPT_SHARE:
        return f"largely written in another language's script (not {lang})"
    clean = normalise(text)
    if not clean:
        return "only symbols or punctuation" if str(text).strip() else None
    squashed = clean.replace(" ", "")
    if clean in ALWAYS or squashed in {p.replace(" ", "") for p in ALWAYS}:
        return "a stock phrase transcribers invent for silence or music"
    if rms is not None and rms < QUIET_RMS and clean in QUIET_ONLY:
        return "a courtesy phrase heard in a quiet slice"
    words = clean.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return "one word repeated over and over"
    match = _REPEAT.search(squashed)
    if match and len(squashed) >= 10 and (match.end() - match.start()) >= 0.8 * len(squashed):
        return "the same syllable repeated over and over"
    return None


def is_spurious(text: str, rms: float | None = None, lang: str | None = None) -> bool:
    return enabled() and reason(text, rms, lang) is not None
