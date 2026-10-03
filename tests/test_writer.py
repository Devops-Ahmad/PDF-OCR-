import json

from bookocr.core.types import PageResult, PageStatus, QualityTier, Region
from bookocr.output.writer import BookOutputWriter


def _result(page_number: int, text: str, *, processing_pass: int = 1) -> PageResult:
    return PageResult(
        book_id="book",
        page_number=page_number,
        text=text,
        confidence=90,
        tier=QualityTier.HIGH,
        status=PageStatus.COMPLETED,
        engine_used="test",
        processing_pass=processing_pass,
        regions=[Region(kind="body", bbox=(0, 0, 100, 20), text=text)],
    )


def test_finalize_uses_latest_page_record_and_writes_public_outputs(tmp_path):
    writer = BookOutputWriter(tmp_path, ".ocr_internal", "===== PAGE {page} =====", "## Page {page}", {})
    writer.write_page(_result(1, "old text"))
    writer.write_page(_result(1, "new text", processing_pass=2))
    writer.finalize_book("book", {"title": "Example"})

    assert "new text" in (tmp_path / "book.txt").read_text(encoding="utf-8")
    assert "old text" not in (tmp_path / "book.txt").read_text(encoding="utf-8")
    assert (tmp_path / "book.md").read_text(encoding="utf-8").startswith("# Example")
    report = json.loads((tmp_path / ".ocr_internal" / "qc_report.json").read_text(encoding="utf-8"))
    assert report["total_pages"] == 1
