"""Shared data types passed between pipeline stages.

Kept dependency-free (no cv2/paddle imports here) so any component can import
this module without pulling in heavy ML libraries transitively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PageStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    REPROCESSING = "REPROCESSING"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLANK = "BLANK"


class RoutingAction(str, Enum):
    ACCEPT = "accept"
    REPROCESS_LOCAL = "reprocess_local"
    ESCALATE_EXTERNAL = "escalate_external"
    FLAG_FOR_REVIEW = "flag_for_review"


class QualityTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CRITICAL = "CRITICAL"


@dataclass
class PageImage:
    """A rasterized page ready for (pre)processing. `array` is a numpy BGR/gray
    image; kept as an opaque object here (typed as object) to avoid importing
    numpy in this module for the rare caller that doesn't need it.
    """

    book_id: str
    page_number: int  # 1-indexed, matches the physical page in the source PDF
    path: str  # where the raster currently lives on disk (work_dir)
    width: int
    height: int
    dpi: int


@dataclass
class Region:
    kind: str  # body | heading | header | footer | page_number | footnote | illustration | watermark | caption | table
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in page-pixel coords
    text: str = ""
    confidence: float | None = None
    reading_order: int | None = None


@dataclass
class EngineResult:
    text: str
    confidence: float | None  # 0-100 native score when the engine genuinely exposes one
    engine_name: str
    engine_version: str
    regions: list[Region] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # engine-native output kept for debugging
    model_revision: str = "unknown"
    output_format: str = "text"  # text | lines | regions | markdown | html
    runtime_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    score: float  # 0-100
    tier: QualityTier
    signals: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PageResult:
    book_id: str
    page_number: int
    text: str
    confidence: float
    tier: QualityTier
    status: PageStatus
    engine_used: str
    processing_pass: int
    regions: list[Region] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine_versions: dict[str, str] = field(default_factory=dict)
    processed_at: str = ""
    duration_s: float = 0.0
    page_width: int = 0
    page_height: int = 0

    def to_jsonl_record(self) -> dict:
        return {
            "page": self.page_number,
            "text": self.text,
            "confidence": round(self.confidence, 2),
            "tier": self.tier.value,
            "status": self.status.value,
            "engine_used": self.engine_used,
            "processing_pass": self.processing_pass,
            "page_width": self.page_width,
            "page_height": self.page_height,
            "regions": [
                {
                    "kind": r.kind,
                    "bbox": list(r.bbox),
                    "text": r.text,
                    "confidence": r.confidence,
                }
                for r in self.regions
            ],
            "warnings": self.warnings,
            "engine_versions": self.engine_versions,
            "processed_at": self.processed_at,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class PageProfile:
    book_id: str
    page_number: int
    source_image_sha256: str
    original_path: str
    processed_path: str
    width: int
    height: int
    dpi: int
    analysis: dict = field(default_factory=dict)
    preprocessing_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "page": self.page_number,
            "source_image_sha256": self.source_image_sha256,
            "original_path": self.original_path,
            "processed_path": self.processed_path,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
            "analysis": self.analysis,
            "preprocessing_applied": self.preprocessing_applied,
        }


@dataclass
class CandidateResult:
    page_number: int
    processing_pass: int
    engine: EngineResult
    quality: QualityReport
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "page": self.page_number,
            "processing_pass": self.processing_pass,
            "engine": {
                "name": self.engine.engine_name,
                "version": self.engine.engine_version,
                "model_revision": self.engine.model_revision,
                "output_format": self.engine.output_format,
                "native_confidence": self.engine.confidence,
                "runtime_s": self.engine.runtime_s,
                "warnings": self.engine.warnings,
                "text": self.engine.text,
                "regions": [
                    {
                        "kind": r.kind,
                        "bbox": list(r.bbox),
                        "text": r.text,
                        "confidence": r.confidence,
                        "reading_order": r.reading_order,
                    }
                    for r in self.engine.regions
                ],
                "raw": self.engine.raw,
            },
            "quality": {
                "score": self.quality.score,
                "tier": self.quality.tier.value,
                "signals": self.quality.signals,
                "warnings": self.quality.warnings,
            },
            "evidence": self.evidence,
        }


@dataclass
class DecisionRecord:
    page_number: int
    action: RoutingAction
    status: PageStatus
    selected_engine: str | None
    processing_pass: int
    reasons: list[str] = field(default_factory=list)
    candidate_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page": self.page_number,
            "action": self.action.value,
            "status": self.status.value,
            "selected_engine": self.selected_engine,
            "processing_pass": self.processing_pass,
            "reasons": self.reasons,
            "candidate_artifacts": self.candidate_artifacts,
        }


@dataclass
class ValidationReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }
