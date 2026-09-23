"""Orchestrates a single book through the pipeline: rasterize -> preprocess
-> OCR Pass 1 -> quality score -> write. Produces exactly two public
artifacts, book.txt and book.md, in the caller-specified output directory.
Everything else written (pages.jsonl, state.db, qc_report.json, manifest.json)
lives under <output>/.ocr_internal/ and exists only to make the tool
reliable, resumable, and debuggable -- never as a competing product output.

Escalation (Pass 2 QARI-OCR, Pass 3 cloud fallback) and real layout detection
(Surya) are later work -- see docs/roadmap.md. `EscalationPolicy` only ever
returns 'accept' or 'flag_for_review' for now; the interface is already in
place so wiring in an actual secondary engine later is additive, not a
rewrite.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import tempfile
from pathlib import Path

import structlog

from bookocr import __version__ as PIPELINE_VERSION
from bookocr.config import Config
from bookocr.core.state import StateStore
from bookocr.core.types import PageResult, PageStatus, QualityTier, Region
from bookocr.engines.paddle_engine import PaddleOCREngine
from bookocr.output.writer import BookOutputWriter
from bookocr.preprocess.adaptive import AdaptivePreprocessor
from bookocr.quality.scorer import HeuristicQualityEvaluator
from bookocr.rasterize.pymupdf_rasterizer import CorruptedPageError, PyMuPDFRasterizer, PyMuPDFSource

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
        logging.basicConfig(level=config.logging.level)

    def convert(self, source_path: str, output_dir: str, force: bool = False) -> None:
        """The one public entry point: PDF in, book.txt + book.md out."""
        source_path = str(Path(source_path).resolve())
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        book_log = log.bind(source=source_path, output=str(output_dir))

        if not self.source.is_readable(source_path):
            book_log.error("source_unreadable")
            raise ValueError(f"Cannot open PDF: {source_path}")

        internal_dir = output_dir / self.cfg.output.internal_dirname
        state = StateStore(internal_dir / "state.db")
        writer = BookOutputWriter(
            output_dir,
            self.cfg.output.internal_dirname,
            self.cfg.output.txt_page_marker,
            self.cfg.output.md_page_heading,
            self.cfg.watermark_filter.model_dump(),
        )

        book_id = "book"  # single book per output dir; state.db is not shared across books
        page_count = self.source.page_count(source_path)
        state.register_book(book_id, source_path, page_count, PIPELINE_VERSION, _config_hash(self.cfg))

        pending = state.pending_pages(book_id) if not force else list(range(1, page_count + 1))
        book_log.info("processing_start", total_pages=page_count, pending_pages=len(pending))

        with tempfile.TemporaryDirectory(prefix="bookocr_") as work_dir:
            for page_number in pending:
                self._process_page(source_path, book_id, page_number, Path(work_dir), state, writer, book_log)

        title = Path(source_path).stem
        self._finalize(book_id, source_path, page_count, title, state, writer, book_log)

    def _process_page(self, source_path, book_id, page_number, work_dir: Path, state: StateStore, writer: BookOutputWriter, book_log) -> None:
        page_log = book_log.bind(page=page_number)
        state.set_page_status(book_id, page_number, PageStatus.PROCESSING)
        t0 = datetime.datetime.now(datetime.UTC)

        try:
            page_img = self.rasterizer.render_page(source_path, page_number, self.cfg.rasterize.dpi, str(work_dir))
        except CorruptedPageError as e:
            page_log.warning("page_corrupted", error=str(e))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return

        analysis = self.preprocessor.analyze(page_img)
        if analysis.get("blank"):
            self._write_and_mark(
                writer, state, book_id, page_number, text="", confidence=100.0, tier=QualityTier.HIGH,
                status=PageStatus.BLANK, engine_used="none", processing_pass=1, regions=[],
                warnings=["blank_page"], t0=t0, page_img=page_img,
            )
            return

        page_img = self.preprocessor.apply(page_img, analysis)

        try:
            result = self.engine.recognize(page_img)
        except Exception as e:  # an engine crash on one page must not kill the book
            page_log.error("ocr_engine_failed", error=str(e))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return

        report = self.evaluator.score(result, page_img)
        status = PageStatus.COMPLETED if report.tier in (QualityTier.HIGH, QualityTier.MEDIUM) else PageStatus.LOW_CONFIDENCE

        self._write_and_mark(
            writer, state, book_id, page_number, text=result.text, confidence=report.score, tier=report.tier,
            status=status, engine_used=self.engine.name, processing_pass=1, regions=result.regions,
            warnings=report.warnings, t0=t0, page_img=page_img,
        )
        page_log.info("page_done", tier=report.tier.value, confidence=report.score)

    def _write_and_mark(
        self, writer: BookOutputWriter, state: StateStore, book_id: str, page_number: int, *, text: str,
        confidence: float, tier: QualityTier, status: PageStatus, engine_used: str, processing_pass: int,
        regions: list[Region], warnings: list[str], t0: datetime.datetime, page_img,
    ) -> None:
        duration = (datetime.datetime.now(datetime.UTC) - t0).total_seconds()
        result = PageResult(
            book_id=book_id, page_number=page_number, text=text, confidence=confidence, tier=tier,
            status=status, engine_used=engine_used, processing_pass=processing_pass, regions=regions,
            warnings=warnings, engine_versions={self.engine.name: self.engine.version},
            processed_at=datetime.datetime.now(datetime.UTC).isoformat(), duration_s=duration,
            page_width=page_img.width, page_height=page_img.height,
        )
        writer.write_page(result)
        state.set_page_status(
            book_id, page_number, status, quality_tier=tier.value, confidence=confidence,
            engine_used=engine_used, processing_pass=processing_pass,
        )

    def _finalize(self, book_id, source_path, page_count, title, state: StateStore, writer: BookOutputWriter, book_log) -> None:
        manifest = {
            "book_id": book_id,
            "title": title,
            "source_path": source_path,
            "page_count": page_count,
            "pipeline_version": PIPELINE_VERSION,
            "config_hash": _config_hash(self.cfg),
            "engine_versions": {self.engine.name: self.engine.version},
            "finalized_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        writer.finalize_book(book_id, manifest)
        progress = state.book_progress(book_id)
        failed = state.failed_pages(book_id)
        if not failed and progress["by_status"].get(PageStatus.PENDING.value, 0) == 0:
            state.mark_book_complete(book_id)
        book_log.info("processing_finalized", progress=progress, failed_count=len(failed))
