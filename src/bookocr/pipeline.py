"""Resumable, evidence-producing Arabic book OCR orchestration.

The public product remains exactly ``book.txt`` and ``book.md``. Version 2
adds a durable audit trail under ``.ocr_internal``: source provenance,
triage, layout, raw engine candidates, routing decisions, attempts, events,
and whole-book validation. OCR engines are acquired through a registry and
run in persistent child processes by default.

Accuracy is never inferred from a model's confidence. The heuristic quality
score only drives conservative routing; measured CER/WER still requires
human ground truth outside this runtime pipeline.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from bookocr import __version__ as PIPELINE_VERSION
from bookocr.config import Config
from bookocr.core.artifacts import ArtifactStore
from bookocr.core.engine_registry import EngineRegistry, EngineRunner
from bookocr.core.provenance import metadata_fingerprint, sha256_file
from bookocr.core.state import StateStore
from bookocr.core.types import (
    CandidateResult,
    DecisionRecord,
    EngineResult,
    PageImage,
    PageResult,
    PageStatus,
    QualityReport,
    QualityTier,
    Region,
    RoutingAction,
)
from bookocr.output.writer import BookOutputWriter
from bookocr.preprocess.adaptive import AdaptivePreprocessor
from bookocr.quality.evidence import collect_evidence
from bookocr.quality.routing import ConservativeEscalationPolicy
from bookocr.quality.scorer import HeuristicQualityEvaluator
from bookocr.rasterize.pymupdf_rasterizer import CorruptedPageError, PyMuPDFRasterizer, PyMuPDFSource
from bookocr.rasterize.text_layer import PDFTextLayerExtractor
from bookocr.validation import WholeBookValidator

log = structlog.get_logger()


def _config_hash(cfg: Config) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode("utf-8")).hexdigest()[:16]


@dataclass
class EscalationCandidate:
    page_image: PageImage
    primary_result: EngineResult
    primary_report: QualityReport
    primary_artifact: str


class BookPipeline:
    def __init__(self, config: Config, *, engine_registry: EngineRegistry | None = None):
        self.cfg = config
        self.source = PyMuPDFSource()
        self.rasterizer = PyMuPDFRasterizer()
        self.preprocessor = AdaptivePreprocessor(config.model_dump())
        self.text_layer_extractor = PDFTextLayerExtractor(config.triage.model_dump())
        self.evaluator = HeuristicQualityEvaluator(config.quality.model_dump())
        self.layout_detector = self._build_layout_detector(config.layout.engine)
        self.engine_registry = engine_registry or EngineRegistry.with_builtins()
        self._primary_runner: EngineRunner | None = None
        self._engine_versions: dict[str, str] = {}
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
        """Convert one PDF and leave a complete, inspectable audit trail."""
        source_path = str(Path(source_path).resolve())
        output_dir_path = Path(output_dir).resolve()
        output_dir_path.mkdir(parents=True, exist_ok=True)
        book_log = log.bind(source=source_path, output=str(output_dir_path))

        if not self.source.is_readable(source_path):
            book_log.error("source_unreadable")
            raise ValueError(f"Cannot open PDF: {source_path}")

        internal_dir = output_dir_path / self.cfg.output.internal_dirname
        state = StateStore(internal_dir / "state.db")
        artifacts = ArtifactStore(internal_dir)
        writer = BookOutputWriter(
            output_dir_path,
            self.cfg.output.internal_dirname,
            self.cfg.output.txt_page_marker,
            self.cfg.output.md_page_heading,
            self.cfg.watermark_filter.model_dump(),
        )
        policy = ConservativeEscalationPolicy(
            escalation_enabled=bool(self.cfg.ocr.escalation.get("enabled")),
            max_normalized_disagreement=float(self.cfg.ocr.routing.get("max_normalized_disagreement", 0.35)),
        )

        book_id = "book"
        page_count = self.source.page_count(source_path)
        source_sha256 = sha256_file(source_path)
        source_fingerprint = metadata_fingerprint(source_path)
        state.register_book(
            book_id,
            source_path,
            page_count,
            PIPELINE_VERSION,
            _config_hash(self.cfg),
            source_fingerprint,
            source_sha256=source_sha256,
        )
        artifacts.record_event(
            "book_registered",
            book_id=book_id,
            source_path=source_path,
            source_sha256=source_sha256,
            page_count=page_count,
            config_hash=_config_hash(self.cfg),
        )

        pending = state.pending_pages(book_id) if not force else list(range(1, page_count + 1))
        book_log.info("processing_start", total_pages=page_count, pending_pages=len(pending))
        artifacts.record_event("processing_started", pending_pages=pending, force=force)

        try:
            with tempfile.TemporaryDirectory(prefix="bookocr_") as work_dir:
                escalation_candidates: dict[int, EscalationCandidate] = {}
                for page_number in pending:
                    candidate = self._process_page(
                        source_path,
                        book_id,
                        page_number,
                        Path(work_dir),
                        state,
                        writer,
                        artifacts,
                        policy,
                        book_log,
                    )
                    if candidate is not None:
                        escalation_candidates[page_number] = candidate

                # Reclaim the primary model before a different worker starts.
                self.engine_registry.close_all()
                if escalation_candidates:
                    self._run_escalation_sweep(
                        escalation_candidates,
                        book_id,
                        state,
                        writer,
                        artifacts,
                        policy,
                        book_log,
                    )
        finally:
            self.engine_registry.close_all()
            self._primary_runner = None

        self._finalize(
            book_id,
            source_path,
            source_sha256,
            page_count,
            Path(source_path).stem,
            state,
            writer,
            artifacts,
            output_dir_path,
            book_log,
        )

    def _process_page(
        self,
        source_path: str,
        book_id: str,
        page_number: int,
        work_dir: Path,
        state: StateStore,
        writer: BookOutputWriter,
        artifacts: ArtifactStore,
        policy: ConservativeEscalationPolicy,
        book_log,
    ) -> EscalationCandidate | None:
        page_log = book_log.bind(page=page_number)
        state.set_page_status(book_id, page_number, PageStatus.PROCESSING)
        artifacts.record_event("page_started", page=page_number)
        started_at = datetime.datetime.now(datetime.UTC)

        try:
            original_page = self.rasterizer.render_page(source_path, page_number, self.cfg.rasterize.dpi, str(work_dir))
        except CorruptedPageError as error:
            page_log.warning("page_corrupted", error=str(error))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(error), bump_retry=True)
            artifacts.record_event("page_failed", page=page_number, stage="rasterize", error=str(error))
            return None

        analysis = self.preprocessor.analyze(original_page)
        if analysis.get("blank"):
            artifacts.write_profile(artifacts.build_profile(original_page, original_page, analysis))
            self._write_and_mark(
                writer,
                state,
                book_id,
                page_number,
                text="",
                confidence=100.0,
                tier=QualityTier.HIGH,
                status=PageStatus.BLANK,
                engine_used="none",
                processing_pass=1,
                regions=[],
                warnings=["blank_page"],
                started_at=started_at,
                page_img=original_page,
                engine_versions={},
            )
            artifacts.write_decision(
                DecisionRecord(
                    page_number=page_number,
                    action=RoutingAction.ACCEPT,
                    status=PageStatus.BLANK,
                    selected_engine=None,
                    processing_pass=1,
                    reasons=["blank_page"],
                )
            )
            artifacts.record_event("page_completed", page=page_number, status=PageStatus.BLANK.value)
            return None

        text_layer_result, text_layer_analysis = self.text_layer_extractor.extract(
            source_path,
            page_number,
            raster_width=original_page.width,
            raster_height=original_page.height,
        )
        analysis["text_layer"] = text_layer_analysis
        page_img = self.preprocessor.apply(original_page, analysis)
        artifacts.write_profile(artifacts.build_profile(original_page, page_img, analysis))

        engine = None if text_layer_analysis["usable"] else self._get_primary_runner()
        selected_engine_name = text_layer_result.engine_name if text_layer_analysis["usable"] else engine.name
        attempt_id = state.begin_attempt(book_id, page_number, 1, selected_engine_name)
        attempt_started = time.monotonic()
        try:
            result = text_layer_result if text_layer_analysis["usable"] else engine.recognize(page_img)
            if not result.runtime_s:
                result.runtime_s = time.monotonic() - attempt_started
        except Exception as error:
            page_log.error("ocr_engine_failed", error=str(error))
            state.finish_attempt(attempt_id, status="FAILED", error=str(error))
            state.set_page_status(book_id, page_number, PageStatus.FAILED, error=str(error), bump_retry=True)
            artifacts.record_event("page_failed", page=page_number, stage="recognition", engine=selected_engine_name, error=str(error))
            return None

        if text_layer_analysis["usable"]:
            layout_provider, layout_warnings = "native-pdf-text-layer", []
        else:
            layout_provider, layout_warnings = self._apply_layout(page_img, result, page_log)
        artifacts.write_layout(page_number, result.regions, provider=layout_provider, warnings=layout_warnings)
        result.text = "\n".join(region.text for region in result.regions)
        report = self.evaluator.score(result, page_img)
        evidence = collect_evidence(result)
        candidate = CandidateResult(page_number, 1, result, report, evidence)
        candidate_path = artifacts.write_candidate(candidate)

        action = policy.next_action(report, 1, evidence)
        if action is RoutingAction.ACCEPT:
            status = PageStatus.COMPLETED
        elif action is RoutingAction.REPROCESS_LOCAL:
            status = PageStatus.LOW_CONFIDENCE
        else:
            status = PageStatus.REVIEW_REQUIRED

        self._write_engine_result(writer, state, book_id, candidate, status, started_at, page_img)
        state.finish_attempt(attempt_id, status=status.value, artifact_path=str(candidate_path))
        artifacts.write_decision(
            DecisionRecord(
                page_number=page_number,
                action=action,
                status=status,
                selected_engine=result.engine_name,
                processing_pass=1,
                reasons=[*report.warnings, *result.warnings],
                candidate_artifacts=[str(candidate_path)],
            )
        )
        artifacts.record_event(
            "page_routed",
            page=page_number,
            engine=result.engine_name,
            action=action.value,
            status=status.value,
            quality_tier=report.tier.value,
        )
        page_log.info("page_done", tier=report.tier.value, quality_score=report.score, action=action.value)

        if action is RoutingAction.REPROCESS_LOCAL:
            return EscalationCandidate(page_img, result, report, str(candidate_path))
        return None

    def _get_primary_runner(self) -> EngineRunner:
        if self._primary_runner is None:
            self._primary_runner = self.engine_registry.create(
                self.cfg.ocr.primary_engine,
                self.cfg.ocr.primary,
                self.cfg.ocr.execution,
            )
            self._engine_versions[self._primary_runner.name] = self._primary_runner.version
        return self._primary_runner

    def _apply_layout(self, page_img: PageImage, result: EngineResult, page_log) -> tuple[str, list[str]]:
        from bookocr.layout.reading_order import order_lines_rtl

        warnings: list[str] = []
        if self.layout_detector is not None:
            try:
                layout_regions = self.layout_detector.detect(page_img)
                from bookocr.layout.surya_layout import assign_lines_to_layout

                result.regions = assign_lines_to_layout(result.regions, layout_regions)
                return getattr(self.layout_detector, "name", "layout"), warnings
            except Exception as error:
                page_log.warning("layout_detection_failed", error=str(error))
                warnings.append(f"layout_detection_failed:{type(error).__name__}")
        result.regions = order_lines_rtl(result.regions)
        return "rtl-row-fallback", warnings

    def _write_engine_result(
        self,
        writer: BookOutputWriter,
        state: StateStore,
        book_id: str,
        candidate: CandidateResult,
        status: PageStatus,
        started_at: datetime.datetime,
        page_img: PageImage,
    ) -> None:
        result = candidate.engine
        self._engine_versions[result.engine_name] = result.engine_version
        self._write_and_mark(
            writer,
            state,
            book_id,
            candidate.page_number,
            text=result.text,
            confidence=candidate.quality.score,
            tier=candidate.quality.tier,
            status=status,
            engine_used=result.engine_name,
            processing_pass=candidate.processing_pass,
            regions=result.regions,
            warnings=[*candidate.quality.warnings, *result.warnings],
            started_at=started_at,
            page_img=page_img,
            engine_versions={result.engine_name: result.engine_version},
        )

    def _write_and_mark(
        self,
        writer: BookOutputWriter,
        state: StateStore,
        book_id: str,
        page_number: int,
        *,
        text: str,
        confidence: float,
        tier: QualityTier,
        status: PageStatus,
        engine_used: str,
        processing_pass: int,
        regions: list[Region],
        warnings: list[str],
        started_at: datetime.datetime,
        page_img: PageImage,
        engine_versions: dict[str, str],
    ) -> None:
        duration = (datetime.datetime.now(datetime.UTC) - started_at).total_seconds()
        result = PageResult(
            book_id=book_id,
            page_number=page_number,
            text=text,
            confidence=confidence,
            tier=tier,
            status=status,
            engine_used=engine_used,
            processing_pass=processing_pass,
            regions=regions,
            warnings=warnings,
            engine_versions=engine_versions,
            processed_at=datetime.datetime.now(datetime.UTC).isoformat(),
            duration_s=duration,
            page_width=page_img.width,
            page_height=page_img.height,
        )
        writer.write_page(result)
        state.set_page_status(
            book_id,
            page_number,
            status,
            quality_tier=tier.value,
            confidence=confidence,
            engine_used=engine_used,
            processing_pass=processing_pass,
        )

    def _run_escalation_sweep(
        self,
        candidates: dict[int, EscalationCandidate],
        book_id: str,
        state: StateStore,
        writer: BookOutputWriter,
        artifacts: ArtifactStore,
        policy: ConservativeEscalationPolicy,
        book_log,
    ) -> None:
        engine_name = self.cfg.ocr.escalation.get("engine", "qari-ocr")
        book_log.info("escalation_start", candidate_count=len(candidates), engine=engine_name)
        artifacts.record_event("escalation_started", candidate_count=len(candidates), engine=engine_name)

        if self.layout_detector is not None:
            from bookocr.layout.surya_layout import shutdown as shutdown_surya

            shutdown_surya()

        escalation_engine = self.engine_registry.create(
            engine_name,
            self.cfg.ocr.escalation,
            self.cfg.ocr.execution,
            escalation=True,
        )
        self._engine_versions[escalation_engine.name] = escalation_engine.version

        for page_number, primary in candidates.items():
            page_log = book_log.bind(page=page_number)
            state.set_page_status(book_id, page_number, PageStatus.REPROCESSING)
            started_at = datetime.datetime.now(datetime.UTC)
            attempt_id = state.begin_attempt(book_id, page_number, 2, escalation_engine.name)
            attempt_started = time.monotonic()
            try:
                result = escalation_engine.recognize(primary.page_image)
                if not result.runtime_s:
                    result.runtime_s = time.monotonic() - attempt_started
            except Exception as error:
                page_log.error("escalation_engine_failed", error=str(error))
                state.finish_attempt(attempt_id, status="FAILED", error=str(error))
                self._write_primary_for_review(
                    primary,
                    writer,
                    state,
                    book_id,
                    started_at,
                    extra_warning=f"escalation_failed:{type(error).__name__}",
                )
                artifacts.write_decision(
                    DecisionRecord(
                        page_number=page_number,
                        action=RoutingAction.FLAG_FOR_REVIEW,
                        status=PageStatus.REVIEW_REQUIRED,
                        selected_engine=primary.primary_result.engine_name,
                        processing_pass=2,
                        reasons=["escalation_engine_failed", str(error)],
                        candidate_artifacts=[primary.primary_artifact],
                    )
                )
                artifacts.record_event("escalation_failed", page=page_number, engine=escalation_engine.name, error=str(error))
                continue

            from bookocr.layout.reading_order import order_lines_rtl

            result.regions = order_lines_rtl(result.regions)
            result.text = "\n".join(region.text for region in result.regions)
            report = self.evaluator.score(result, primary.page_image)
            evidence = collect_evidence(result, baseline_text=primary.primary_result.text)
            candidate = CandidateResult(page_number, 2, result, report, evidence)
            candidate_path = artifacts.write_candidate(candidate)
            action = policy.next_action(report, 2, evidence)

            if action is RoutingAction.ACCEPT:
                status = PageStatus.COMPLETED
                self._write_engine_result(writer, state, book_id, candidate, status, started_at, primary.page_image)
                selected_engine = result.engine_name
                reasons = [*report.warnings, *result.warnings]
            else:
                status = PageStatus.REVIEW_REQUIRED
                self._write_primary_for_review(
                    primary,
                    writer,
                    state,
                    book_id,
                    started_at,
                    extra_warning="second_engine_not_safely_accepted",
                )
                selected_engine = primary.primary_result.engine_name
                reasons = ["second_engine_not_safely_accepted", *report.warnings, *result.warnings]

            state.finish_attempt(attempt_id, status=status.value, artifact_path=str(candidate_path))
            artifacts.write_decision(
                DecisionRecord(
                    page_number=page_number,
                    action=action,
                    status=status,
                    selected_engine=selected_engine,
                    processing_pass=2,
                    reasons=reasons,
                    candidate_artifacts=[primary.primary_artifact, str(candidate_path)],
                )
            )
            artifacts.record_event(
                "escalation_completed",
                page=page_number,
                action=action.value,
                status=status.value,
                normalized_disagreement=evidence.get("normalized_disagreement"),
            )
            page_log.info("escalation_done", tier=report.tier.value, action=action.value, status=status.value)

        self.engine_registry.close_all()

    def _write_primary_for_review(
        self,
        primary: EscalationCandidate,
        writer: BookOutputWriter,
        state: StateStore,
        book_id: str,
        started_at: datetime.datetime,
        *,
        extra_warning: str,
    ) -> None:
        review_report = QualityReport(
            score=primary.primary_report.score,
            tier=primary.primary_report.tier,
            signals=dict(primary.primary_report.signals),
            warnings=[*primary.primary_report.warnings, extra_warning],
        )
        candidate = CandidateResult(
            primary.page_image.page_number,
            2,
            primary.primary_result,
            review_report,
            collect_evidence(primary.primary_result),
        )
        self._write_engine_result(
            writer,
            state,
            book_id,
            candidate,
            PageStatus.REVIEW_REQUIRED,
            started_at,
            primary.page_image,
        )

    def _finalize(
        self,
        book_id: str,
        source_path: str,
        source_sha256: str,
        page_count: int,
        title: str,
        state: StateStore,
        writer: BookOutputWriter,
        artifacts: ArtifactStore,
        output_dir: Path,
        book_log,
    ) -> None:
        manifest = {
            "schema_version": 2,
            "book_id": book_id,
            "title": title,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "page_count": page_count,
            "pipeline_version": PIPELINE_VERSION,
            "config_hash": _config_hash(self.cfg),
            "engine_versions": self._engine_versions,
            "finalized_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        writer.finalize_book(book_id, manifest)

        validation = WholeBookValidator(writer.internal_dir, page_count, output_dir=output_dir).validate_book(book_id)
        artifacts.write_validation(validation)
        progress = state.book_progress(book_id)
        failed = state.failed_pages(book_id)

        if not validation.valid:
            state.mark_book_status(book_id, "FAILED_VALIDATION")
            artifacts.record_event("book_validation_failed", errors=validation.errors, warnings=validation.warnings)
            book_log.error("processing_validation_failed", errors=validation.errors)
            raise RuntimeError("Book output failed integrity validation: " + "; ".join(validation.errors))

        if validation.stats.get("review_required_pages"):
            state.mark_book_status(book_id, "REVIEW_REQUIRED")
            final_status = "REVIEW_REQUIRED"
        else:
            state.mark_book_complete(book_id)
            final_status = "COMPLETED"

        artifacts.record_event(
            "book_finalized",
            status=final_status,
            progress=progress,
            failed_count=len(failed),
            validation=validation.to_dict(),
        )
        book_log.info(
            "processing_finalized",
            status=final_status,
            progress=progress,
            failed_count=len(failed),
            validation_warnings=validation.warnings,
        )
