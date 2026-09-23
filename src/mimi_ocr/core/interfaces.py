"""Abstract interfaces for every replaceable pipeline component.

This is the extensibility contract: a new OCR engine, a new preprocessor, a
new layout detector, or a cloud fallback provider must implement one of these
and nothing else in the pipeline should need to change. Concrete
implementations live under mimi_ocr/{engines,rasterize,preprocess,layout,
quality,output}/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from mimi_ocr.core.types import EngineResult, PageImage, PageResult, QualityReport, Region


class DocumentSource(ABC):
    """Turns a source path into an iterable of pages. Today this is always a
    local scanned PDF; the abstraction exists so a future source (an image
    folder, a different container format) doesn't require touching anything
    downstream.
    """

    @abstractmethod
    def page_count(self, source_path: str) -> int: ...

    @abstractmethod
    def is_readable(self, source_path: str) -> bool: ...


class Rasterizer(ABC):
    @abstractmethod
    def render_page(self, source_path: str, page_number: int, dpi: int, out_dir: str) -> PageImage:
        """Render a single 1-indexed page to an image file. Must raise a
        recoverable exception (not crash the process) on a corrupted page so
        the caller can mark it FAILED and continue.
        """
        ...


class Preprocessor(ABC):
    @abstractmethod
    def analyze(self, page: PageImage) -> dict:
        """Cheap analysis pass: skew angle, contrast, noise score, blank-page
        ink ratio, effective DPI. Drives which corrections get applied — never
        apply every operation blindly.
        """
        ...

    @abstractmethod
    def apply(self, page: PageImage, analysis: dict) -> PageImage:
        """Apply only the corrections `analysis` says are needed, writing a
        new image and returning a PageImage pointing at it. Must not mutate
        the original raster in place, so a later pass can retry with
        different parameters.
        """
        ...


class LayoutDetector(ABC):
    @abstractmethod
    def detect(self, page: PageImage) -> list[Region]:
        """Segment the page into typed, ordered regions (body, heading,
        header, footer, page_number, footnote, illustration, watermark,
        caption, table) with reading order assigned. A no-op implementation
        may return a single `body` region covering the whole page.
        """
        ...


class OCREngine(ABC):
    name: str
    version: str

    @abstractmethod
    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        """Run recognition, either over the whole page (regions=None) or
        per-region if a layout pass already segmented it. Must return a
        native confidence score, not a guess.
        """
        ...


class QualityEvaluator(ABC):
    @abstractmethod
    def score(self, result: EngineResult, page: PageImage) -> QualityReport: ...


class EscalationPolicy(ABC):
    @abstractmethod
    def next_action(self, report: QualityReport, processing_pass: int) -> str:
        """Returns one of: 'accept', 'reprocess_local', 'escalate_cloud',
        'flag_for_review'. Owns the multi-pass decision logic so it can be
        tuned/replaced without touching engines or scoring.
        """
        ...


class OutputWriter(ABC):
    @abstractmethod
    def write_page(self, result: PageResult) -> None: ...

    @abstractmethod
    def finalize_book(self, book_id: str, manifest: dict) -> None: ...


class Validator(ABC):
    @abstractmethod
    def validate_book(self, book_id: str) -> QualityReport:
        """Post-hoc, whole-book sanity check: page-count match, duplicate
        pages, monotonic page numbers, no gaps — run once processing
        finishes, before the book is considered done.
        """
        ...


class JobManager(ABC):
    @abstractmethod
    def enqueue(self, book_id: str, source_path: str) -> None: ...

    @abstractmethod
    def pending_pages(self, book_id: str) -> Iterator[int]: ...
