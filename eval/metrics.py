"""Error-rate and translation-quality metrics with bootstrap confidence intervals.

Dependency-free so they run anywhere the relay does. Corpus-level, like the
published tools: errors are summed over the whole test set and then divided,
so one long utterance weighs more than one short one.
"""
from __future__ import annotations

import collections
import random
import unicodedata
from collections.abc import Sequence

from eval.evaluate import edit_distance


def normalize(text: str, keep_spaces: bool = True) -> str:
    """NFC, case-folded, punctuation removed (any script: Burmese, Khmer and Lao
    punctuation included), whitespace collapsed. keep_spaces=False also drops spaces,
    which is how character error rate is conventionally scored."""
    text = unicodedata.normalize("NFC", text).casefold()
    text = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    text = " ".join(text.split())
    return text if keep_spaces else text.replace(" ", "")


def cer_counts(reference: str, hypothesis: str) -> tuple[int, int]:
    """(character edits, reference characters) for one utterance."""
    ref, hyp = normalize(reference, keep_spaces=False), normalize(hypothesis, keep_spaces=False)
    return edit_distance(list(ref), list(hyp)), len(ref)


def wer_counts(reference: str, hypothesis: str) -> tuple[int, int]:
    """(word edits, reference words) for one utterance. Meaningless for scripts written
    without spaces; report character error rate for those."""
    ref, hyp = normalize(reference).split(), normalize(hypothesis).split()
    return edit_distance(ref, hyp), len(ref)


def rate(counts: Sequence[tuple[int, int]]) -> float:
    errors, total = sum(c[0] for c in counts), sum(c[1] for c in counts)
    return errors / total if total else 0.0


def bootstrap_ci(counts: Sequence[tuple], statistic, samples: int = 1000, seed: int = 0,
                 confidence: float = 0.95) -> tuple[float, float]:
    """Percentile bootstrap over utterances. Seeded, so a rerun gives the same interval."""
    if not counts:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(counts)
    values = sorted(statistic([counts[rng.randrange(n)] for _ in range(n)]) for _ in range(samples))
    low = values[int(((1 - confidence) / 2) * samples)]
    high = values[min(samples - 1, int((1 - (1 - confidence) / 2) * samples))]
    return low, high


# ---- chrF / chrF++ -----------------------------------------------------------------

def _char_ngrams(text: str, order: int) -> collections.Counter:
    text = "".join(text.split())   # chrF ignores whitespace in character n-grams
    return collections.Counter(text[i:i + order] for i in range(len(text) - order + 1))


_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\]^_`{|}~")


def _word_tokens(text: str) -> list[str]:
    """Words with one leading or trailing ASCII punctuation mark split off as its own
    token - the same rule the reference chrF++ implementation (sacrebleu) uses, so scores
    are comparable with published ones."""
    tokens: list[str] = []
    for word in text.split():
        if len(word) == 1:
            tokens.append(word)
        elif word[-1] in _PUNCTUATION:
            tokens += [word[:-1], word[-1]]
        elif word[0] in _PUNCTUATION:
            tokens += [word[0], word[1:]]
        else:
            tokens.append(word)
    return tokens


def _word_ngrams(words: list[str], order: int) -> collections.Counter:
    return collections.Counter(tuple(words[i:i + order]) for i in range(len(words) - order + 1))


def _triple(hyp: collections.Counter, ref: collections.Counter) -> tuple[int, int, int]:
    # Hypothesis n-grams only count where the reference has some of that order
    # (sacrebleu's rule), so a hypothesis longer than its reference is not over-penalised.
    return (sum(hyp.values()) if ref else 0, sum(ref.values()), sum((ref & hyp).values()))


def chrf_stats(reference: str, hypothesis: str, char_order: int = 6, word_order: int = 2) -> list[tuple[int, int, int]]:
    """Per n-gram order: (hypothesis n-grams, reference n-grams, matching n-grams)."""
    stats = []
    for order in range(1, char_order + 1):
        ref, hyp = _char_ngrams(reference, order), _char_ngrams(hypothesis, order)
        stats.append(_triple(hyp, ref))
    if word_order:
        ref_words, hyp_words = _word_tokens(reference), _word_tokens(hypothesis)
        for order in range(1, word_order + 1):
            ref, hyp = _word_ngrams(ref_words, order), _word_ngrams(hyp_words, order)
            stats.append(_triple(hyp, ref))
    return stats


def chrf_from_stats(per_sentence: Sequence[Sequence[tuple[int, int, int]]], beta: float = 2.0) -> float:
    """Corpus chrF from summed per-sentence statistics (0-100). Precision and recall are
    averaged over the n-gram orders first and F is computed once, as sacrebleu 2.x does."""
    if not per_sentence:
        return 0.0
    totals = [tuple(sum(s[i][j] for s in per_sentence) for j in range(3)) for i in range(len(per_sentence[0]))]
    factor = beta ** 2
    precision_sum = recall_sum = 0.0
    effective = 0
    for n_hyp, n_ref, n_match in totals:
        if n_hyp > 0 and n_ref > 0:
            precision_sum += n_match / n_hyp
            recall_sum += n_match / n_ref
            effective += 1
    if not effective:
        return 0.0
    precision, recall = precision_sum / effective, recall_sum / effective
    if precision + recall == 0:
        return 0.0
    return 100 * (1 + factor) * precision * recall / (factor * precision + recall)


def chrf(references: Sequence[str], hypotheses: Sequence[str], word_order: int = 2) -> float:
    """chrF++ by default (word_order=2); word_order=0 gives plain chrF."""
    return chrf_from_stats([chrf_stats(r, h, word_order=word_order) for r, h in zip(references, hypotheses)])
