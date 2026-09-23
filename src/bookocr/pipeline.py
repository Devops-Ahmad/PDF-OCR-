"""Orchestrates a single book through the pipeline in two sweeps:

  Sweep 1 (every pending page): rasterize -> preprocess -> layout detection
  -> PaddleOCR -> quality score -> write. Cheap, and per Phase 0 benchmarking
  (docs/phase0_findings.md) already scores HIGH on most of this library.

  Sweep 2 (only pages sweep 1 scored LOW/CRITICAL, when
  ocr.escalation.enabled): QARI-OCR, a small Arabic-specialized VLM, re-reads
  just those pages -- on CPU by default (see engines/qari_engine.py for why:
  the GPU path doesn't reliably fit in 4GB alongside Surya's own footprint).
  Sweep 2 only starts after sweep 1 finishes the whole book, both because the
  escalation set isn't known until then and to keep the two GPU-touching
  stages from overlapping in time even though they don't currently contend
  for the same resource.

Produces exactly two public artifacts, book.txt and book.md, in the
caller-specified output directory. Everything else written (pages.jsonl,
state.db, qc_report.json, manifest.json) lives under
<output>/.ocr_internal/ and exists only to make the tool reliable,
resumable, and debuggable -- never as a competing product output.

Reading order is treated as part of OCR correctness, not optional polish --
see docs/phase0_findings.md for the scrambled-dialogue defect layout
detection fixes. Pass 3 cloud fallback is not implemented yet.
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
from bookocr.core.types import PageImage, PageResult, PageStatus, QualityTier, Region
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
        self.layout_detector = self._build_layout_detector(config.layout.engine)
        logging.basicConfig(level=config.logging.level)

    @staticmethod
    def _build_layout_detector(engine_name: str):
        if engine_name == "none":
            return None
        if engine_name == "surya":
            from bookocr.layout.surya_layout import SuryaLayoutDetector

            return SuryaLayoutDetector()
        raise ValueError(f"Unknown layout.engine: {engine_name!r}")

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
            escalation_candidates: dict[int, PageImage] = {}
            for page_number in pending:
                page_img = self._process_page(source_path, book_id, page_number, Path(work_dir), state, writer, book_log)
                if page_img is not None:
                    escalation_candidates[page_number] = page_img

            if escalation_candidates and self.cfg.ocr.escalation.get("enabled"):
                self._run_escalation_sweep(escalation_candidates, book_id, state, writer, book_log)

        title = Path(source_path).stem
        self._finalize(book_id, source_path, page_count, title, state, writer, book_log)

    def _process_page(self, source_path, book_id, page_number, work_dir: Path, state: StateStore, writer: BookOutputWriter, book_log) -> "PageImage | None":
        """Returns the (preprocessed) PageImage if this page scored
        LOW/CRITICAL and escalation is enabled -- the caller collects these
        for the sweep-2 pass -- else None.
        """
        page_log = book_log.bind(page=page_number)
        state.set_page_status(book_id, page_number, PageStatus.PROCESSING)
        t0 = datetime.datetime.now(datetime.UTC)

        try:
            page_img = self.rasterizer.render_page(source_path, page_number, self.cfg.rasterize.dpi, str(work_dir))
        except CorruptedPageError as e:
            page_log.warning("page_corrupted", error=str(e))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return None

        analysis = self.preprocessor.analyze(page_img)
        if analysis.get("blank"):
            self._write_and_mark(
                writer, state, book_id, page_number, text="", confidence=100.0, tier=QualityTier.HIGH,
                status=PageStatus.BLANK, engine_used="none", processing_pass=1, regions=[],
                warnings=["blank_page"], t0=t0, page_img=page_img,
                engine_versions={},
            )
            return None

        page_img = self.preprocessor.apply(page_img, analysis)

        try:
            result = self.engine.recognize(page_img)
        except Exception as e:  # an engine crash on one page must not kill the book
            page_log.error("ocr_engine_failed", error=str(e))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(e), bump_retry=True)
            return None

        from bookocr.layout.reading_order import order_lines_rtl

        if self.layout_detector is not None:
            try:
                layout_regions = self.layout_detector.detect(page_img)
                from bookocr.layout.surya_layout import assign_lines_to_layout

                result.regions = assign_lines_to_layout(result.regions, layout_regions)
            except Exception as e:
                # Layout is a structure/ordering improvement, not a hard
                # dependency for having text at all -- a layout-model crash
                # must not lose the page's OCR output. Still apply the
                # layout-independent RTL row fix rather than falling all the
                # way back to PaddleOCR's raw (known-scrambling-prone) order.
                page_log.warning("layout_detection_failed", error=str(e))
                result.regions = order_lines_rtl(result.regions)
        else:
            # No typed layout blocks, but the RTL row-splitting fix is
            # layout-independent -- always apply it, not just when Surya runs.
            result.regions = order_lines_rtl(result.regions)

        result.text = "\n".join(r.text for r in result.regions)

        report = self.evaluator.score(result, page_img)
        needs_escalation = report.tier in (QualityTier.LOW, QualityTier.CRITICAL)
        status = PageStatus.LOW_CONFIDENCE if needs_escalation else PageStatus.COMPLETED

        self._write_and_mark(
            writer, state, book_id, page_number, text=result.text, confidence=report.score, tier=report.tier,
            status=status, engine_used=self.engine.name, processing_pass=1, regions=result.regions,
            warnings=report.warnings, t0=t0, page_img=page_img,
            engine_versions={self.engine.name: self.engine.version},
        )
        page_log.info("page_done", tier=report.tier.value, confidence=report.score)

        escalation_enabled = self.cfg.ocr.escalation.get("enabled")
        return page_img if (needs_escalation and escalation_enabled) else None

    def _write_and_mark(
        self, writer: BookOutputWriter, state: StateStore, book_id: str, page_number: int, *, text: str,
        confidence: float, tier: QualityTier, status: PageStatus, engine_used: str, processing_pass: int,
        regions: list[Region], warnings: list[str], t0: datetime.datetime, page_img, engine_versions: dict[str, str],
    ) -> None:
        duration = (datetime.datetime.now(datetime.UTC) - t0).total_seconds()
        result = PageResult(
            book_id=book_id, page_number=page_number, text=text, confidence=confidence, tier=tier,
            status=status, engine_used=engine_used, processing_pass=processing_pass, regions=regions,
            warnings=warnings, engine_versions=engine_versions,
            processed_at=datetime.datetime.now(datetime.UTC).isoformat(), duration_s=duration,
            page_width=page_img.width, page_height=page_img.height,
        )
        writer.write_page(result)
        state.set_page_status(
            book_id, page_number, status, quality_tier=tier.value, confidence=confidence,
            engine_used=engine_used, processing_pass=processing_pass,
        )

    def _run_escalation_sweep(
        self, candidates: dict[int, PageImage], book_id: str, state: StateStore, writer: BookOutputWriter, book_log
    ) -> None:
        """Pass 2: re-read every page sweep 1 scored LOW/CRITICAL with
        QARI-OCR (CPU by default -- see engines/qari_engine.py). Runs only
        after sweep 1 finishes the whole book.
        """
        from bookocr.engines.qari_engine import QariOCREngine
        from bookocr.layout.reading_order import order_lines_rtl

        book_log.info("escalation_start", candidate_count=len(candidates))

        if self.layout_detector is not None:
            from bookocr.layout.surya_layout import shutdown as shutdown_surya

            shutdown_surya()

        escalation_engine = QariOCREngine(self.cfg.ocr.escalation)

        for page_number, page_img in candidates.items():
            page_log = book_log.bind(page=page_number)
            state.set_page_status(book_id, page_number, PageStatus.REPROCESSING)
            t0 = datetime.datetime.now(datetime.UTC)

            try:
                result = escalation_engine.recognize(page_img)
            except Exception as e:
                # Keep the Pass-1 result (already written); a Pass-2 crash on
                # a hard page must not lose the page's only usable text.
                page_log.error("escalation_engine_failed", error=str(e))
                state.set_page_status(book_id, page_number, PageStatus.LOW_CONFIDENCE, error=str(e))
                continue

            result.regions = order_lines_rtl(result.regions)
            result.text = "\n".join(r.text for r in result.regions)
            report = self.evaluator.score(result, page_img)

            # Never silently downgrade: if Pass 2 is still LOW/CRITICAL, flag
            # for human review rather than looping or pretending it's fine.
            status = PageStatus.COMPLETED if report.tier in (QualityTier.HIGH, QualityTier.MEDIUM) else PageStatus.REVIEW_REQUIRED

            self._write_and_mark(
                writer, state, book_id, page_number, text=result.text, confidence=report.score, tier=report.tier,
                status=status, engine_used=escalation_engine.name, processing_pass=2, regions=result.regions,
                warnings=report.warnings, t0=t0, page_img=page_img,
                engine_versions={escalation_engine.name: escalation_engine.version},
            )
            page_log.info("escalation_done", tier=report.tier.value, confidence=report.score, status=status.value)

        from bookocr.engines.qari_engine import shutdown as shutdown_qari

        shutdown_qari()

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
