"""Orchestrates a single book through the pipeline stages that exist so far
(Phase 0/1/2: rasterize -> preprocess -> OCR Pass 1 -> quality score -> write).

Escalation (Pass 2 QARI-OCR, Pass 3 cloud fallback) and real layout detection
(Surya) are later phases — see docs/roadmap.md. For now, `EscalationPolicy`
only ever returns 'accept' or 'flag_for_review'; the interface is already in
place so wiring in an actual secondary engine later is additive, not a
rewrite.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from pathlib import Path

import structlog

from mimi_ocr import __version__ as PIPELINE_VERSION
from mimi_ocr.config import Config
from mimi_ocr.core.state import StateStore
from mimi_ocr.core.types import PageResult, PageStatus, QualityTier, Region
from mimi_ocr.engines.paddle_engine import PaddleOCREngine
from mimi_ocr.preprocess.adaptive import AdaptivePreprocessor
from mimi_ocr.quality.scorer import HeuristicQualityEvaluator
from mimi_ocr.rasterize.pymupdf_rasterizer import CorruptedPageError, PyMuPDFRasterizer, PyMuPDFSource, book_id_for
from mimi_ocr.output.writer import JsonlOutputWriter

log = structlog.get_logger()


def _config_hash(cfg: Config) -> str:
    return hashlib.sha1(cfg.model_dump_json().encode("utf-8")).hexdigest()[:12]


class BookPipeline:
    def __init__(self, config: Config):
        self.cfg = config
        self.source = PyMuPDFSource()
        self.rasterizer = PyMuPDFRasterizer()
        self.preprocessor = AdaptivePreprocessor(config.model_dump())
        self.engine = PaddleOCREngine(config.ocr.primary)
        self.evaluator = HeuristicQualityEvaluator(config.quality.model_dump())
        self.writer = JsonlOutputWriter(config.resolve_path(config.paths.processed_dir))
        self.state = StateStore(config.resolve_path(config.paths.state_db))

        logging.basicConfig(level=config.logging.level)

    def process_book(self, source_path: str, force: bool = False) -> str:
        source_path = str(Path(source_path).resolve())
        book_id = book_id_for(source_path)
        book_log = log.bind(book_id=book_id, source=source_path)

        if not self.source.is_readable(source_path):
            book_log.error("source_unreadable")
            raise ValueError(f"Cannot open PDF: {source_path}")

        page_count = self.source.page_count(source_path)
        self.state.register_book(book_id, source_path, page_count, PIPELINE_VERSION, _config_hash(self.cfg))

        pending = self.state.pending_pages(book_id) if not force else list(range(1, page_count + 1))
        book_log.info("processing_start", total_pages=page_count, pending_pages=len(pending))

        work_dir = self.cfg.resolve_path(self.cfg.paths.work_dir) / book_id
        work_dir.mkdir(parents=True, exist_ok=True)

        for page_number in pending:
            self._process_page(source_path, book_id, page_number, work_dir, book_log)

        self._finalize(book_id, source_path, page_count, book_log)
        return book_id

    def _process_page(self, source_path: str, book_id: str, page_number: int, work_dir: Path, book_log) -> None:
        page_log = book_log.bind(page=page_number)
        self.state.set_page_status(book_id, page_number, PageStatus.PROCESSING)
        t0 = datetime.datetime.now(datetime.UTC)

        try:
            page_img = self.rasterizer.render_page(source_path, page_number, self.cfg.rasterize.dpi, str(work_dir))
        except CorruptedPageError as e:
            page_log.warning("page_corrupted", error=str(e))
            self.state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return

        analysis = self.preprocessor.analyze(page_img)
        if analysis.get("blank"):
            self._write_and_mark(
                book_id, page_number, text="", confidence=100.0, tier=QualityTier.HIGH,
                status=PageStatus.BLANK, engine_used="none", processing_pass=1, regions=[],
                warnings=["blank_page"], t0=t0,
            )
            if not self.cfg.rasterize.keep_rendered_pages:
                Path(page_img.path).unlink(missing_ok=True)
            return

        page_img = self.preprocessor.apply(page_img, analysis)

        try:
            result = self.engine.recognize(page_img)
        except Exception as e:  # an engine crash on one page must not kill the book
            page_log.error("ocr_engine_failed", error=str(e))
            self.state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return

        report = self.evaluator.score(result, page_img)
        status = PageStatus.COMPLETED if report.tier in (QualityTier.HIGH, QualityTier.MEDIUM) else PageStatus.LOW_CONFIDENCE

        self._write_and_mark(
            book_id, page_number, text=result.text, confidence=report.score, tier=report.tier,
            status=status, engine_used=self.engine.name, processing_pass=1, regions=result.regions,
            warnings=report.warnings, t0=t0,
        )

        if not self.cfg.rasterize.keep_rendered_pages:
            Path(page_img.path).unlink(missing_ok=True)

        page_log.info("page_done", tier=report.tier.value, confidence=report.score)

    def _write_and_mark(
        self, book_id: str, page_number: int, *, text: str, confidence: float, tier: QualityTier,
        status: PageStatus, engine_used: str, processing_pass: int, regions: list[Region],
        warnings: list[str], t0: datetime.datetime,
    ) -> None:
        duration = (datetime.datetime.now(datetime.UTC) - t0).total_seconds()
        result = PageResult(
            book_id=book_id, page_number=page_number, text=text, confidence=confidence, tier=tier,
            status=status, engine_used=engine_used, processing_pass=processing_pass, regions=regions,
            warnings=warnings, engine_versions={self.engine.name: self.engine.version},
            processed_at=datetime.datetime.now(datetime.UTC).isoformat(), duration_s=duration,
        )
        self.writer.write_page(result)
        self.state.set_page_status(
            book_id, page_number, status, quality_tier=tier.value, confidence=confidence,
            engine_used=engine_used, processing_pass=processing_pass,
        )

    def _finalize(self, book_id: str, source_path: str, page_count: int, book_log) -> None:
        manifest = {
            "book_id": book_id,
            "source_path": source_path,
            "page_count": page_count,
            "pipeline_version": PIPELINE_VERSION,
            "config_hash": _config_hash(self.cfg),
            "engine_versions": {self.engine.name: self.engine.version},
            "finalized_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        self.writer.finalize_book(book_id, manifest)
        progress = self.state.book_progress(book_id)
        failed = self.state.failed_pages(book_id)
        if not failed and progress["by_status"].get(PageStatus.PENDING.value, 0) == 0:
            self.state.mark_book_complete(book_id)
        book_log.info("processing_finalized", progress=progress, failed_count=len(failed))
