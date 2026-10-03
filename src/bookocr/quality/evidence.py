"""Observable evidence that complements, but never impersonates, accuracy."""

from __future__ import annotations

import re

from rapidfuzz.distance import Levenshtein

from bookocr.core.types import EngineResult

_PUNCTUATION = re.compile(r"[.!?:;،؛؟«»()\[\]…\-—]")
_DIGITS = re.compile(r"[0-9٠-٩]")
_LATIN = re.compile(r"[A-Za-z]")
_HTML = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


def collect_evidence(result: EngineResult, *, baseline_text: str | None = None) -> dict:
    text = result.text or ""
    evidence = {
        "character_count": len(text),
        "line_count": len([line for line in text.splitlines() if line.strip()]),
        "region_count": len(result.regions),
        "punctuation_count": len(_PUNCTUATION.findall(text)),
        "digit_count": len(_DIGITS.findall(text)),
        "latin_count": len(_LATIN.findall(text)),
        "html_tag_count": len(_HTML.findall(text)),
        "native_confidence_available": result.confidence is not None,
    }
    if baseline_text is not None:
        evidence["normalized_disagreement"] = Levenshtein.normalized_distance(baseline_text, text)
        baseline_len = max(len(baseline_text), 1)
        evidence["length_ratio_to_baseline"] = len(text) / baseline_len
    return evidence
