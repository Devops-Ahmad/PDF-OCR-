"""Durable internal audit artifacts for the routed OCR pipeline."""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from bookocr.core.provenance import sha256_file
from bookocr.core.types import CandidateResult, DecisionRecord, PageProfile, Region

_append_lock = threading.Lock()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


class ArtifactStore:
    def __init__(self, internal_dir: str | Path):
        self.internal_dir = Path(internal_dir)
        self.pages_dir = self.internal_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.internal_dir / "events.jsonl"

    @staticmethod
    def write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(_json_safe(value), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)

    def page_dir(self, page_number: int) -> Path:
        path = self.pages_dir / f"{page_number:06d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record_event(self, event: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "event": event,
            **_json_safe(payload),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with _append_lock, open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write_profile(self, profile: PageProfile) -> Path:
        path = self.page_dir(profile.page_number) / "triage.json"
        self.write_json_atomic(path, profile.to_dict())
        return path

    def build_profile(self, original, processed, analysis: dict) -> PageProfile:
        return PageProfile(
            book_id=original.book_id,
            page_number=original.page_number,
            source_image_sha256=sha256_file(original.path),
            original_path=original.path,
            processed_path=processed.path,
            width=processed.width,
            height=processed.height,
            dpi=processed.dpi,
            analysis=_json_safe(analysis),
            preprocessing_applied=Path(original.path) != Path(processed.path),
        )

    def write_layout(self, page_number: int, regions: list[Region], *, provider: str, warnings: list[str]) -> Path:
        path = self.page_dir(page_number) / "layout.json"
        payload = {
            "page": page_number,
            "provider": provider,
            "warnings": warnings,
            "regions": [
                {
                    "kind": r.kind,
                    "bbox": list(r.bbox),
                    "text": r.text,
                    "confidence": r.confidence,
                    "reading_order": r.reading_order,
                }
                for r in regions
            ],
        }
        self.write_json_atomic(path, payload)
        return path

    def write_candidate(self, candidate: CandidateResult) -> Path:
        safe_engine = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate.engine.engine_name).strip("-") or "engine"
        path = self.page_dir(candidate.page_number) / "candidates" / f"pass-{candidate.processing_pass}-{safe_engine}.json"
        self.write_json_atomic(path, candidate.to_dict())
        return path

    def write_decision(self, decision: DecisionRecord) -> Path:
        path = self.page_dir(decision.page_number) / "decision.json"
        self.write_json_atomic(path, decision.to_dict())
        return path

    def write_validation(self, report: Any) -> Path:
        path = self.internal_dir / "validation_report.json"
        value = report.to_dict() if hasattr(report, "to_dict") else report
        self.write_json_atomic(path, value)
        return path
