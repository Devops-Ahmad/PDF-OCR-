"""Conservative routing policy separated from OCR and scoring."""

from __future__ import annotations

from bookocr.core.interfaces import EscalationPolicy
from bookocr.core.types import QualityReport, QualityTier, RoutingAction


class ConservativeEscalationPolicy(EscalationPolicy):
    def __init__(self, *, escalation_enabled: bool, max_normalized_disagreement: float = 0.35):
        self.escalation_enabled = escalation_enabled
        self.max_normalized_disagreement = max_normalized_disagreement

    def next_action(self, report: QualityReport, processing_pass: int, evidence: dict | None = None) -> RoutingAction:
        evidence = evidence or {}
        if evidence.get("html_tag_count", 0) > 0:
            return RoutingAction.FLAG_FOR_REVIEW
        disagreement = evidence.get("normalized_disagreement")
        if processing_pass > 1 and disagreement is not None and disagreement > self.max_normalized_disagreement:
            return RoutingAction.FLAG_FOR_REVIEW
        if report.tier in (QualityTier.HIGH, QualityTier.MEDIUM):
            return RoutingAction.ACCEPT
        if processing_pass == 1 and self.escalation_enabled:
            return RoutingAction.REPROCESS_LOCAL
        return RoutingAction.FLAG_FOR_REVIEW
