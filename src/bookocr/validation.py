"""Whole-book integrity validation before a conversion is marked complete."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bookocr.core.interfaces import Validator
from bookocr.core.types import PageStatus, ValidationReport

_FINAL_PAGE_STATUSES = {PageStatus.COMPLETED.value, PageStatus.REVIEW_REQUIRED.value, PageStatus.BLANK.value}


class WholeBookValidator(Validator):
    def __init__(self, internal_dir: str | Path, expected_page_count: int, *, output_dir: str | Path | None = None):
        self.internal_dir = Path(internal_dir)
        self.expected_page_count = expected_page_count
        self.output_dir = Path(output_dir) if output_dir is not None else self.internal_dir.parent

    def validate_book(self, book_id: str) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        pages_path = self.internal_dir / "pages.jsonl"
        latest: dict[int, dict] = {}
        record_counts: dict[int, int] = {}
        malformed = 0

        if not pages_path.exists():
            return ValidationReport(False, errors=["missing pages.jsonl"], stats={"expected_pages": self.expected_page_count})

        with open(pages_path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    page_number = int(record["page"])
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    malformed += 1
                    errors.append(f"malformed pages.jsonl record at line {line_number}")
                    continue
                record_counts[page_number] = record_counts.get(page_number, 0) + 1
                latest[page_number] = record

        expected = set(range(1, self.expected_page_count + 1))
        actual = set(latest)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            errors.append(f"missing canonical page records: {missing[:20]}")
        if extra:
            errors.append(f"out-of-range page records: {extra[:20]}")

        non_final = sorted(
            page for page, record in latest.items() if record.get("status") not in _FINAL_PAGE_STATUSES
        )
        if non_final:
            errors.append(f"non-final page statuses: {non_final[:20]}")

        review_pages = sorted(
            page for page, record in latest.items() if record.get("status") == PageStatus.REVIEW_REQUIRED.value
        )
        if review_pages:
            warnings.append(f"pages require human review: {review_pages[:20]}")

        stuck_attempts: list[dict] = []
        state_path = self.internal_dir / "state.db"
        if state_path.exists():
            try:
                with sqlite3.connect(state_path) as connection:
                    rows = connection.execute(
                        """SELECT page_number, processing_pass, engine_used
                           FROM attempts WHERE book_id=? AND status='PROCESSING'
                           ORDER BY page_number, processing_pass""",
                        (book_id,),
                    ).fetchall()
                stuck_attempts = [
                    {"page": int(page), "processing_pass": int(processing_pass), "engine": engine}
                    for page, processing_pass, engine in rows
                ]
                if stuck_attempts:
                    errors.append(f"unfinished processing attempts: {stuck_attempts[:20]}")
            except sqlite3.Error as error:
                errors.append(f"cannot validate state.db attempts: {error}")

        for filename in ("book.txt", "book.md"):
            path = self.output_dir / filename
            if not path.exists():
                errors.append(f"missing public output: {filename}")
            elif path.stat().st_size == 0:
                errors.append(f"empty public output: {filename}")

        stats = {
            "book_id": book_id,
            "expected_pages": self.expected_page_count,
            "canonical_pages": len(latest),
            "malformed_records": malformed,
            "superseded_records": sum(max(count - 1, 0) for count in record_counts.values()),
            "review_required_pages": review_pages,
            "unfinished_attempts": stuck_attempts,
        }
        return ValidationReport(not errors, errors=errors, warnings=warnings, stats=stats)
