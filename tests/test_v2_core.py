import json

import pymupdf

from bookocr.core.artifacts import ArtifactStore
from bookocr.core.provenance import sha256_file
from bookocr.core.state import StateStore
from bookocr.core.types import (
    CandidateResult,
    DecisionRecord,
    EngineResult,
    PageStatus,
    QualityReport,
    QualityTier,
    Region,
    RoutingAction,
)
from bookocr.quality.routing import ConservativeEscalationPolicy
from bookocr.rasterize.text_layer import PDFTextLayerExtractor
from bookocr.validation import WholeBookValidator


def test_sha256_uses_file_content(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"first revision")
    first = sha256_file(source)
    source.write_bytes(b"second revision")
    assert sha256_file(source) != first


def test_state_records_content_hash_and_attempts(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.register_book("book", "/book.pdf", 1, "0.1", "cfg", "metadata", source_sha256="abc")
    attempt_id = state.begin_attempt("book", 1, 1, "fake")
    state.finish_attempt(attempt_id, status="COMPLETED", artifact_path="candidate.json")

    assert state.page_attempts("book", 1)[0]["artifact_path"] == "candidate.json"

    import pytest

    with pytest.raises(ValueError, match="content hash"):
        state.register_book("book", "/book.pdf", 1, "0.1", "cfg", "metadata", source_sha256="changed")


def test_artifact_store_writes_candidate_decision_and_event(tmp_path):
    store = ArtifactStore(tmp_path / ".ocr_internal")
    engine = EngineResult(
        text="قال الرجل نعم",
        confidence=None,
        engine_name="vlm/test",
        engine_version="1",
        regions=[Region(kind="body", bbox=(0, 0, 10, 10), text="قال الرجل نعم")],
        raw={"opaque": object()},
    )
    quality = QualityReport(score=75, tier=QualityTier.MEDIUM)
    candidate_path = store.write_candidate(CandidateResult(1, 1, engine, quality, {"line_count": 1}))
    store.write_decision(
        DecisionRecord(1, RoutingAction.ACCEPT, PageStatus.COMPLETED, "vlm/test", 1, candidate_artifacts=[str(candidate_path)])
    )
    store.record_event("test_event", page=1)

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["engine"]["native_confidence"] is None
    assert candidate["engine"]["raw"]["opaque"].startswith("<object object")
    assert json.loads((store.page_dir(1) / "decision.json").read_text(encoding="utf-8"))["action"] == "accept"
    assert json.loads(store.events_path.read_text(encoding="utf-8"))["event"] == "test_event"


def test_conservative_policy_routes_low_disagreement_and_html():
    policy = ConservativeEscalationPolicy(escalation_enabled=True, max_normalized_disagreement=0.35)
    low = QualityReport(score=40, tier=QualityTier.LOW)
    high = QualityReport(score=90, tier=QualityTier.HIGH)

    assert policy.next_action(low, 1) is RoutingAction.REPROCESS_LOCAL
    assert policy.next_action(high, 2, {"normalized_disagreement": 0.8}) is RoutingAction.FLAG_FOR_REVIEW
    assert policy.next_action(high, 2, {"html_tag_count": 1}) is RoutingAction.FLAG_FOR_REVIEW
    assert policy.next_action(high, 2, {"normalized_disagreement": 0.1}) is RoutingAction.ACCEPT


def test_whole_book_validator_distinguishes_review_from_corruption(tmp_path):
    internal = tmp_path / ".ocr_internal"
    internal.mkdir()
    records = [
        {"page": 1, "status": "COMPLETED"},
        {"page": 2, "status": "REVIEW_REQUIRED"},
    ]
    (internal / "pages.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    (tmp_path / "book.txt").write_text("text", encoding="utf-8")
    (tmp_path / "book.md").write_text("markdown", encoding="utf-8")

    report = WholeBookValidator(internal, 2, output_dir=tmp_path).validate_book("book")
    assert report.valid
    assert report.stats["review_required_pages"] == [2]

    broken = WholeBookValidator(internal, 3, output_dir=tmp_path).validate_book("book")
    assert not broken.valid
    assert any("missing canonical" in error for error in broken.errors)


def test_pdf_text_layer_is_used_only_when_it_passes_configured_gates(tmp_path):
    source = tmp_path / "searchable.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "This searchable PDF contains enough words for a conservative native text check.")
    document.save(source)
    document.close()

    permissive = PDFTextLayerExtractor(
        {
            "prefer_usable_text_layer": True,
            "text_layer_min_chars": 20,
            "text_layer_min_words": 5,
            "text_layer_min_arabic_ratio": 0.0,
            "text_layer_max_replacement_ratio": 0.01,
        }
    )
    result, analysis = permissive.extract(str(source), 1, raster_width=1190, raster_height=1684)
    assert analysis["usable"] is True
    assert result.engine_name == "pdf-text-layer"
    assert "searchable PDF" in result.text

    arabic_only = PDFTextLayerExtractor(
        {
            "prefer_usable_text_layer": True,
            "text_layer_min_chars": 20,
            "text_layer_min_words": 5,
            "text_layer_min_arabic_ratio": 0.5,
            "text_layer_max_replacement_ratio": 0.01,
        }
    )
    rejected, rejected_analysis = arabic_only.extract(str(source), 1, raster_width=1190, raster_height=1684)
    assert rejected_analysis["usable"] is False
    assert "text_layer_rejected" in rejected.warnings


def test_validator_rejects_unfinished_engine_attempt(tmp_path):
    internal = tmp_path / ".ocr_internal"
    state = StateStore(internal / "state.db")
    state.register_book("book", "/book.pdf", 1, "0.1", "cfg", "metadata", source_sha256="abc")
    state.begin_attempt("book", 1, 1, "fake")
    (internal / "pages.jsonl").write_text(
        json.dumps({"page": 1, "status": "COMPLETED"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "book.txt").write_text("text", encoding="utf-8")
    (tmp_path / "book.md").write_text("markdown", encoding="utf-8")

    report = WholeBookValidator(internal, 1, output_dir=tmp_path).validate_book("book")
    assert report.valid is False
    assert report.stats["unfinished_attempts"][0]["engine"] == "fake"
