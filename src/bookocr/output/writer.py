"""Produces the two public artifacts -- book.txt and book.md -- plus an
internal, non-product record used for resumability/debugging/QC.

Directory layout under the output dir given on the CLI:

    <output>/
        book.txt                       <- public product
        book.md                        <- public product
        .ocr_internal/
            pages.jsonl                 <- append-only, one record per page (raw OCR, unfiltered)
            qc_report.json              <- quality stats, not a product output
            state.db                    <- resumability (core/state.py)

pages.jsonl is the append-only source of truth written during processing (so
a crash mid-book never corrupts pages already OCR'd). book.txt/book.md are
*derived* from it at finalize time and are always fully regenerable -- if the
watermark filter or the markdown format changes, rerunning finalize from
pages.jsonl reproduces the product outputs without re-running OCR.
"""

from __future__ import annotations

import json
from pathlib import Path

from bookocr.core.interfaces import OutputWriter
from bookocr.core.types import PageResult
from bookocr.postprocess.watermark import detect_watermark_lines

_EXCLUDED_REGION_KINDS = {"header", "footer", "page_number", "watermark"}


class BookOutputWriter(OutputWriter):
    def __init__(self, output_dir: str | Path, internal_dirname: str, txt_marker: str, md_heading: str, watermark_cfg: dict):
        self.output_dir = Path(output_dir)
        self.internal_dir = self.output_dir / internal_dirname
        self.internal_dir.mkdir(parents=True, exist_ok=True)
        self.txt_marker = txt_marker
        self.md_heading = md_heading
        self.watermark_cfg = watermark_cfg

    @property
    def pages_jsonl_path(self) -> Path:
        return self.internal_dir / "pages.jsonl"

    def write_page(self, result: PageResult) -> None:
        with open(self.pages_jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_jsonl_record(), ensure_ascii=False) + "\n")

    def finalize_book(self, book_id: str, manifest: dict) -> None:
        with open(self.internal_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        pages = self._read_pages()
        watermark_lines = (
            detect_watermark_lines(
                pages,
                bottom_band_fraction=self.watermark_cfg.get("bottom_band_fraction", 0.15),
                min_page_occurrences=self.watermark_cfg.get("min_page_occurrences", 3),
                min_page_fraction=self.watermark_cfg.get("min_page_fraction", 0.3),
            )
            if self.watermark_cfg.get("enabled", True)
            else set()
        )

        self._write_txt(pages, watermark_lines)
        self._write_md(pages, watermark_lines, title=manifest.get("title", book_id))
        self._write_qc_report(pages, watermark_lines)

    def _read_pages(self) -> list[dict]:
        if not self.pages_jsonl_path.exists():
            return []
        pages = []
        with open(self.pages_jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    pages.append(json.loads(line))
        pages.sort(key=lambda p: p["page"])
        return pages

    def _page_body_lines(self, page: dict, watermark_lines: set[str]) -> list[str]:
        lines = []
        for region in page.get("regions", []):
            if region["kind"] in _EXCLUDED_REGION_KINDS:
                continue
            text = region.get("text", "").strip()
            if not text or text in watermark_lines:
                continue
            lines.append(text)
        if not lines and page.get("text"):
            # No region breakdown available (shouldn't normally happen) -- fall
            # back to the whole-page text rather than silently dropping content.
            lines = [line for line in page["text"].split("\n") if line.strip() and line.strip() not in watermark_lines]
        return lines

    def _write_txt(self, pages: list[dict], watermark_lines: set[str]) -> None:
        chunks = []
        for page in pages:
            marker = self.txt_marker.format(page=page["page"])
            body = "\n".join(self._page_body_lines(page, watermark_lines))
            chunks.append(f"{marker}\n\n{body}\n")
        with open(self.output_dir / "book.txt", "w", encoding="utf-8") as f:
            f.write("\n\n".join(chunks) + "\n")

    def _write_md(self, pages: list[dict], watermark_lines: set[str], title: str) -> None:
        chunks = [f"# {title}\n"]
        for page in pages:
            heading = self.md_heading.format(page=page["page"])
            body = "\n\n".join(self._page_body_lines(page, watermark_lines))
            chunks.append(f"{heading}\n\n{body}\n")
        with open(self.output_dir / "book.md", "w", encoding="utf-8") as f:
            f.write("\n".join(chunks) + "\n")

    def _write_qc_report(self, pages: list[dict], watermark_lines: set[str]) -> None:
        tier_counts: dict[str, int] = {}
        flagged = []
        for p in pages:
            tier_counts[p["tier"]] = tier_counts.get(p["tier"], 0) + 1
            if p["tier"] in ("LOW", "CRITICAL") or p["warnings"]:
                flagged.append({"page": p["page"], "tier": p["tier"], "confidence": p["confidence"], "warnings": p["warnings"]})

        confidences = [p["confidence"] for p in pages]
        report = {
            "total_pages": len(pages),
            "tier_distribution": tier_counts,
            "mean_confidence": round(sum(confidences) / len(confidences), 2) if confidences else 0,
            "min_confidence": min(confidences) if confidences else 0,
            "flagged_pages": flagged,
            "flagged_count": len(flagged),
            "watermark_lines_filtered": sorted(watermark_lines),
        }
        with open(self.internal_dir / "qc_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
