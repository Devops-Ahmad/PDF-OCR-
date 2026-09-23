"""Page-oriented, append-only output writer.

book.jsonl is the canonical machine-readable record (one line per page,
append-only so a crash never corrupts pages already written). book.md is a
derived, regenerable, Claude-facing reading view built from book.jsonl at
finalize time — never treated as a source of truth. manifest.json and
qc_report.json are written once, at finalize.
"""

from __future__ import annotations

import json
from pathlib import Path

from mimi_ocr.core.interfaces import OutputWriter
from mimi_ocr.core.types import PageResult


class JsonlOutputWriter(OutputWriter):
    def __init__(self, processed_dir: str | Path):
        self.processed_dir = Path(processed_dir)

    def _book_dir(self, book_id: str) -> Path:
        d = self.processed_dir / book_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_page(self, result: PageResult) -> None:
        book_dir = self._book_dir(result.book_id)
        jsonl_path = book_dir / "book.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_jsonl_record(), ensure_ascii=False) + "\n")

    def finalize_book(self, book_id: str, manifest: dict) -> None:
        book_dir = self._book_dir(book_id)

        with open(book_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        pages = self._read_pages(book_dir)
        self._write_markdown(book_dir, pages)
        self._write_qc_report(book_dir, pages)

    def _read_pages(self, book_dir: Path) -> list[dict]:
        jsonl_path = book_dir / "book.jsonl"
        if not jsonl_path.exists():
            return []
        pages = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    pages.append(json.loads(line))
        pages.sort(key=lambda p: p["page"])
        return pages

    def _write_markdown(self, book_dir: Path, pages: list[dict]) -> None:
        lines = []
        for p in pages:
            lines.append(f"<!-- page:{p['page']:05d} confidence:{p['confidence']} tier:{p['tier']} -->")
            body_text = "\n".join(
                r["text"] for r in p.get("regions", []) if r["kind"] not in ("header", "footer", "page_number", "watermark")
            ) or p["text"]
            lines.append(body_text)
            lines.append("")
        with open(book_dir / "book.md", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_qc_report(self, book_dir: Path, pages: list[dict]) -> None:
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
        }
        with open(book_dir / "qc_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
