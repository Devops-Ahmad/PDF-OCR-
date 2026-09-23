"""Accuracy metrics for measuring OCR output against a reference text.

Character/word error rates are computed on lightly normalized text: NFC,
whitespace collapsed to single spaces. Two views are reported because they
answer different questions:

  strict    -- exact characters, diacritics included. This is the fidelity
               number: "did we transcribe what is printed".
  loose     -- diacritics (tashkeel) and tatweel removed. Separates "wrong
               letters" from "missed/extra optional marks".

Punctuation preservation is reported on its own, because a page can score a
low CER while still dropping every colon and parenthesis -- exactly the
failure seen in the Phase 0 sample, and a stated fidelity requirement.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from rapidfuzz.distance import Levenshtein

_TASHKEEL_RE = re.compile("[ً-ٰٟۖ-ۭـ]")
_PUNCT_CHARS = set(".,:;!?()[]{}\"'«»…-–—،؛؟")


def normalize(text: str, *, loose: bool = False) -> str:
    text = unicodedata.normalize("NFC", text)
    if loose:
        text = _TASHKEEL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def cer(reference: str, hypothesis: str, *, loose: bool = False) -> float:
    ref, hyp = normalize(reference, loose=loose), normalize(hypothesis, loose=loose)
    if not ref:
        return 0.0 if not hyp else 1.0
    return Levenshtein.distance(ref, hyp) / len(ref)


def wer(reference: str, hypothesis: str, *, loose: bool = False) -> float:
    ref = normalize(reference, loose=loose).split(" ")
    hyp = normalize(hypothesis, loose=loose).split(" ")
    if not ref or ref == [""]:
        return 0.0 if not hyp or hyp == [""] else 1.0
    return Levenshtein.distance(ref, hyp) / len(ref)


def punctuation_recall(reference: str, hypothesis: str) -> float | None:
    """Fraction of the reference's punctuation marks (as a multiset) found in
    the hypothesis. None when the reference has no punctuation to preserve.
    """
    ref = Counter(c for c in reference if c in _PUNCT_CHARS)
    total = sum(ref.values())
    if total == 0:
        return None
    hyp = Counter(c for c in hypothesis if c in _PUNCT_CHARS)
    kept = sum(min(count, hyp[c]) for c, count in ref.items())
    return kept / total


def page_report(reference: str, hypothesis: str) -> dict:
    return {
        "cer_strict": cer(reference, hypothesis),
        "cer_loose": cer(reference, hypothesis, loose=True),
        "wer_strict": wer(reference, hypothesis),
        "wer_loose": wer(reference, hypothesis, loose=True),
        "punct_recall": punctuation_recall(reference, hypothesis),
        "ref_chars": len(normalize(reference)),
    }
