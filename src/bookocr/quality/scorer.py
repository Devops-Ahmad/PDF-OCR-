"""Page confidence scoring.

Combines the OCR engine's own confidence with a handful of Arabic-aware
heuristics into a single 0-100 score. None of these signals is trustworthy
alone (engine confidence can be high on fluent-looking garbage; a dictionary
check alone can't see layout damage) — the point is a cheap ensemble that
correlates well enough with "would a human accept this page" to drive
routing, not a claim of linguistic ground truth.

`dictionary_hit_rate` is deliberately a lightweight function-word heuristic,
not a real Arabic morphological analyzer (e.g. CAMeL Tools) — that's a
documented Phase 2+ upgrade candidate, not something to fake precision on
now. It still catches the main failure mode it needs to catch: a page that
degenerated into repeated symbols or Latin-mojibake will score near zero on
this signal even though such a page might still get a deceptively high raw
engine confidence.
"""

from __future__ import annotations

import re
import unicodedata

from bookocr.core.interfaces import QualityEvaluator
from bookocr.core.types import EngineResult, PageImage, QualityReport, QualityTier

# High-frequency Arabic function words / particles. Cheap stand-in for a real
# dictionary lookup: real prose is dense with these; garbled OCR output isn't.
_COMMON_WORDS = frozenset(
    """
    في من على إلى عن مع هذا هذه هذان هؤلاء ذلك تلك التي الذي الذين
    و أو ثم لكن لا لن لم ما لو إن أن إذا كان كانت يكون تكون
    قال قالت يقول قد لقد كل بعض غير أي حتى بين عند لدى نحو
    هو هي هم هن أنا أنت أنتم نحن كنت كنا يكونون
    الله رب يا أيها ألا إلا بل سوف س ب ل ك
    """.split()
)

_ARABIC_RE = re.compile(r"[؀-ۿ]")
_ALLOWED_NONARABIC_RE = re.compile(r"[A-Za-z0-9٠-٩\s.,!?:;\"'()\[\]«»…\-—]")


class HeuristicQualityEvaluator(QualityEvaluator):
    def __init__(self, config: dict):
        self.weights = config.get("weights", {})
        self.thresholds = config.get("thresholds", {"high": 80, "medium": 60})

    def score(self, result: EngineResult, page: PageImage) -> QualityReport:
        text = result.text
        warnings: list[str] = []

        signals = {
            "engine_confidence": result.confidence,
            "arabic_char_ratio": self._arabic_char_ratio(text) * 100,
            "dictionary_hit_rate": self._dictionary_hit_rate(text) * 100,
            "length_anomaly": self._length_score(text) * 100,
            "garbage_symbol_ratio": (1 - self._garbage_ratio(text)) * 100,
        }

        total_weight = sum(self.weights.values()) or 1.0
        score = sum(signals.get(k, 0.0) * w for k, w in self.weights.items()) / total_weight

        if signals["arabic_char_ratio"] < 30:
            warnings.append("low_arabic_content")
        if signals["garbage_symbol_ratio"] < 70:
            warnings.append("high_garbage_symbol_density")
        if len(text.strip()) < 20:
            warnings.append("near_empty_page")
        if self._has_repeated_line(text):
            warnings.append("repeated_line_detected")

        tier = self._tier(score)
        return QualityReport(score=round(score, 2), tier=tier, signals=signals, warnings=warnings)

    def _tier(self, score: float) -> QualityTier:
        if score >= self.thresholds.get("high", 80):
            return QualityTier.HIGH
        if score >= self.thresholds.get("medium", 60):
            return QualityTier.MEDIUM
        if score >= 30:
            return QualityTier.LOW
        return QualityTier.CRITICAL

    @staticmethod
    def _arabic_char_ratio(text: str) -> float:
        non_space = [c for c in text if not c.isspace()]
        if not non_space:
            return 0.0
        arabic = sum(1 for c in non_space if _ARABIC_RE.match(c))
        return arabic / len(non_space)

    @staticmethod
    def _dictionary_hit_rate(text: str) -> float:
        tokens = [unicodedata.normalize("NFC", t) for t in re.findall(r"[؀-ۿ]+", text)]
        if len(tokens) < 5:
            return 0.5  # not enough signal either way
        hits = sum(1 for t in tokens if t in _COMMON_WORDS)
        # Real prose is typically 25-40% function words; scale so that range
        # maps to a healthy score without demanding an exact match.
        rate = hits / len(tokens)
        return min(1.0, rate / 0.25)

    @staticmethod
    def _length_score(text: str) -> float:
        length = len(text.strip())
        if length == 0:
            return 0.0
        if length < 40:
            return length / 40
        return 1.0

    @staticmethod
    def _garbage_ratio(text: str) -> float:
        non_space = [c for c in text if not c.isspace()]
        if not non_space:
            return 0.0
        allowed = sum(1 for c in non_space if _ARABIC_RE.match(c) or _ALLOWED_NONARABIC_RE.match(c))
        return 1 - (allowed / len(non_space))

    @staticmethod
    def _has_repeated_line(text: str) -> bool:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) < 4:
            return False
        seen: dict[str, int] = {}
        for line in lines:
            seen[line] = seen.get(line, 0) + 1
        return any(count >= 3 for count in seen.values())
