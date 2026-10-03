import json

import pymupdf
from click.testing import CliRunner

from bookocr.cli.main import cli
from bookocr.config import load_config
from bookocr.core.engine_registry import EngineRegistry, EngineSpec
from bookocr.core.types import EngineResult, Region
from bookocr.pipeline import BookPipeline


class PipelineFakeEngine:
    name = "pipeline-fake"
    version = "fixture-1"

    def __init__(self, config):
        self.text = "قال الرجل في هذا المكان ثم عاد إلى البيت"

    def recognize(self, page, regions=None):
        region = Region(kind="body", bbox=(10, 10, page.width - 10, 50), text=self.text, confidence=99, reading_order=0)
        return EngineResult(
            text=self.text,
            confidence=99,
            engine_name=self.name,
            engine_version=self.version,
            regions=[region],
            model_revision="fixture",
            output_format="lines",
        )


class MustNotStartEngine:
    def __init__(self, config):
        raise AssertionError("OCR engine must not load when the PDF text layer is usable")


def test_v2_pipeline_writes_auditable_artifacts_without_real_ocr(tmp_path):
    source = tmp_path / "fixture.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(source)
    document.close()

    cfg = load_config(
        cli_overrides={
            "layout.engine": "none",
            "ocr.primary_engine": "pipeline-fake",
            "ocr.execution.mode": "in_process",
            "triage.blank_page_ink_ratio_threshold": 0.0,
        }
    )
    registry = EngineRegistry()
    registry.register(EngineSpec("pipeline-fake", "test_pipeline_v2:PipelineFakeEngine"))
    output = tmp_path / "output"

    BookPipeline(cfg, engine_registry=registry).convert(str(source), str(output))

    internal = output / ".ocr_internal"
    assert (output / "book.txt").exists()
    assert "قال الرجل" in (output / "book.txt").read_text(encoding="utf-8")
    assert json.loads((internal / "manifest.json").read_text(encoding="utf-8"))["schema_version"] == 2
    assert len(json.loads((internal / "manifest.json").read_text(encoding="utf-8"))["source_sha256"]) == 64
    assert json.loads((internal / "validation_report.json").read_text(encoding="utf-8"))["valid"] is True
    assert (internal / "pages" / "000001" / "triage.json").exists()
    assert (internal / "pages" / "000001" / "layout.json").exists()
    assert (internal / "pages" / "000001" / "decision.json").exists()
    assert list((internal / "pages" / "000001" / "candidates").glob("*.json"))

    runner = CliRunner()
    audit_result = runner.invoke(cli, ["audit", str(output), "--page", "1"])
    assert audit_result.exit_code == 0
    assert "pipeline-fake" in audit_result.output
    validation_result = runner.invoke(cli, ["validate", str(output)])
    assert validation_result.exit_code == 0
    assert '"valid": true' in validation_result.output


def test_usable_pdf_text_layer_bypasses_ocr_engine_startup(tmp_path):
    source = tmp_path / "searchable.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "This PDF already contains a complete and reusable searchable text layer.")
    document.save(source)
    document.close()

    cfg = load_config(
        cli_overrides={
            "layout.engine": "none",
            "ocr.primary_engine": "must-not-start",
            "ocr.execution.mode": "in_process",
            "triage.blank_page_ink_ratio_threshold": 0.0,
            "triage.text_layer_min_chars": 20,
            "triage.text_layer_min_words": 5,
            "triage.text_layer_min_arabic_ratio": 0.0,
        }
    )
    registry = EngineRegistry()
    registry.register(EngineSpec("must-not-start", "test_pipeline_v2:MustNotStartEngine"))
    output = tmp_path / "output"

    BookPipeline(cfg, engine_registry=registry).convert(str(source), str(output))

    decision = json.loads(
        (output / ".ocr_internal" / "pages" / "000001" / "decision.json").read_text(encoding="utf-8")
    )
    assert decision["selected_engine"] == "pdf-text-layer"
    assert "searchable text layer" in (output / "book.txt").read_text(encoding="utf-8")
