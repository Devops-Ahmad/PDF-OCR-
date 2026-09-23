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
    confidence: float  # 0-100, native engine confidence rescaled
    engine_name: str
    engine_version: str
    regions: list[Region] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # engine-native output kept for debugging


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

    def to_jsonl_record(self) -> dict:
        return {
            "page": self.page_number,
            "text": self.text,
            "confidence": round(self.confidence, 2),
            "tier": self.tier.value,
            "status": self.status.value,
            "engine_used": self.engine_used,
            "processing_pass": self.processing_pass,
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
