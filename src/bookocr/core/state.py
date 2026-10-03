"""Page-level state tracking for resumability.

Deliberately SQLite, not a JSON file: a book can have 1000+ pages and the
library is meant to grow to thousands of books, so state updates must be
atomic single-row writes, not full-file rewrites. A JSON `_state.json` sounds
fine at 101 books but stops being fine at 10,000; SQLite is fine at both.

One state.db per book, living at <output>/.ocr_internal/state.db, one table,
keyed by (book_id, page_number) (book_id is always the fixed string "book"
here since each output directory holds exactly one book). This is the
source of truth for "what still needs doing" -- pages.jsonl is the source of
truth for "what the text is".
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from bookocr.core.types import PageStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    quality_tier TEXT,
    confidence REAL,
    engine_used TEXT,
    processing_pass INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, page_number)
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(book_id, status);

CREATE TABLE IF NOT EXISTS books (
    book_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    page_count INTEGER,
    source_fingerprint TEXT,
    source_sha256 TEXT,
    pipeline_version TEXT,
    config_hash TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    processing_pass INTEGER NOT NULL,
    engine_used TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    artifact_path TEXT,
    FOREIGN KEY (book_id, page_number) REFERENCES pages(book_id, page_number)
);
CREATE INDEX IF NOT EXISTS idx_attempts_page ON attempts(book_id, page_number, processing_pass);
"""

_lock = threading.Lock()


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
            if "source_fingerprint" not in columns:
                conn.execute("ALTER TABLE books ADD COLUMN source_fingerprint TEXT")
            if "source_sha256" not in columns:
                conn.execute("ALTER TABLE books ADD COLUMN source_sha256 TEXT")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")  # readers don't block the writer
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def register_book(
        self,
        book_id: str,
        source_path: str,
        page_count: int,
        pipeline_version: str,
        config_hash: str,
        source_fingerprint: str,
        source_sha256: str | None = None,
    ) -> None:
        import datetime

        with _lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT source_path, page_count, source_fingerprint, source_sha256 FROM books WHERE book_id=?", (book_id,)
            ).fetchone()
            if existing is not None:
                existing_path, existing_page_count, existing_fingerprint, existing_sha256 = existing
                if existing_path != source_path or existing_page_count != page_count:
                    raise ValueError(
                        "Output directory already belongs to a different PDF or page count. "
                        "Choose an empty --output directory."
                    )
                if existing_fingerprint is not None and existing_fingerprint != source_fingerprint:
                    raise ValueError(
                        "The source PDF has changed since this output directory was created. "
                        "Choose an empty --output directory."
                    )
                if source_sha256 is not None and existing_sha256 is not None and existing_sha256 != source_sha256:
                    raise ValueError(
                        "The source PDF content hash has changed since this output directory was created. "
                        "Choose an empty --output directory."
                    )
            conn.execute(
                """INSERT INTO books (book_id, source_path, page_count, source_fingerprint, source_sha256, pipeline_version, config_hash, started_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PROCESSING')
                   ON CONFLICT(book_id) DO UPDATE SET
                       page_count=excluded.page_count,
                       source_fingerprint=COALESCE(books.source_fingerprint, excluded.source_fingerprint),
                       source_sha256=COALESCE(books.source_sha256, excluded.source_sha256),
                       pipeline_version=excluded.pipeline_version,
                       config_hash=excluded.config_hash,
                       status='PROCESSING',
                       completed_at=NULL""",
                (
                    book_id,
                    source_path,
                    page_count,
                    source_fingerprint,
                    source_sha256,
                    pipeline_version,
                    config_hash,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO pages (book_id, page_number, status, updated_at)
                   VALUES (?, ?, ?, ?)""",
                [
                    (book_id, pno, PageStatus.PENDING.value, datetime.datetime.now(datetime.UTC).isoformat())
                    for pno in range(1, page_count + 1)
                ],
            )

    def begin_attempt(self, book_id: str, page_number: int, processing_pass: int, engine_used: str) -> int:
        import datetime

        with _lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO attempts
                   (book_id, page_number, processing_pass, engine_used, status, started_at)
                   VALUES (?, ?, ?, ?, 'PROCESSING', ?)""",
                (
                    book_id,
                    page_number,
                    processing_pass,
                    engine_used,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        error: str | None = None,
        artifact_path: str | None = None,
    ) -> None:
        import datetime

        with _lock, self._connect() as conn:
            conn.execute(
                """UPDATE attempts SET status=?, completed_at=?, error=?, artifact_path=?
                   WHERE attempt_id=?""",
                (status, datetime.datetime.now(datetime.UTC).isoformat(), error, artifact_path, attempt_id),
            )

    def page_attempts(self, book_id: str, page_number: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT attempt_id, processing_pass, engine_used, status, started_at,
                          completed_at, error, artifact_path
                   FROM attempts WHERE book_id=? AND page_number=? ORDER BY attempt_id""",
                (book_id, page_number),
            ).fetchall()
        keys = ("attempt_id", "processing_pass", "engine_used", "status", "started_at", "completed_at", "error", "artifact_path")
        return [dict(zip(keys, row)) for row in rows]

    def set_page_status(
        self,
        book_id: str,
        page_number: int,
        status: PageStatus,
        *,
        quality_tier: str | None = None,
        confidence: float | None = None,
        engine_used: str | None = None,
        processing_pass: int | None = None,
        error: str | None = None,
        bump_retry: bool = False,
    ) -> None:
        import datetime

        with _lock, self._connect() as conn:
            row = conn.execute(
                "SELECT retry_count FROM pages WHERE book_id=? AND page_number=?", (book_id, page_number)
            ).fetchone()
            retry_count = (row[0] if row else 0) + (1 if bump_retry else 0)
            conn.execute(
                """UPDATE pages SET status=?, quality_tier=COALESCE(?, quality_tier),
                   confidence=COALESCE(?, confidence), engine_used=COALESCE(?, engine_used),
                   processing_pass=COALESCE(?, processing_pass), last_error=?,
                   retry_count=?, updated_at=?
                   WHERE book_id=? AND page_number=?""",
                (
                    status.value,
                    quality_tier,
                    confidence,
                    engine_used,
                    processing_pass,
                    error,
                    retry_count,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                    book_id,
                    page_number,
                ),
            )

    def pending_pages(self, book_id: str) -> list[int]:
        """Pages not yet COMPLETED (includes PENDING, FAILED, LOW_CONFIDENCE
        awaiting escalation). This is what a resume operation re-queues.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT page_number FROM pages WHERE book_id=? AND status NOT IN (?, ?, ?)
                   ORDER BY page_number""",
                (book_id, PageStatus.COMPLETED.value, PageStatus.BLANK.value, PageStatus.REVIEW_REQUIRED.value),
            ).fetchall()
            return [r[0] for r in rows]

    def book_progress(self, book_id: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM pages WHERE book_id=? GROUP BY status", (book_id,)
            ).fetchall()
            total = conn.execute("SELECT page_count FROM books WHERE book_id=?", (book_id,)).fetchone()
            return {
                "total_pages": total[0] if total else 0,
                "by_status": dict(rows),
            }

    def mark_book_complete(self, book_id: str) -> None:
        import datetime

        with _lock, self._connect() as conn:
            conn.execute(
                "UPDATE books SET status='COMPLETED', completed_at=? WHERE book_id=?",
                (datetime.datetime.now(datetime.UTC).isoformat(), book_id),
            )

    def mark_book_status(self, book_id: str, status: str) -> None:
        import datetime

        completed_at = datetime.datetime.now(datetime.UTC).isoformat() if status in {"COMPLETED", "REVIEW_REQUIRED"} else None
        with _lock, self._connect() as conn:
            conn.execute(
                "UPDATE books SET status=?, completed_at=? WHERE book_id=?",
                (status, completed_at, book_id),
            )

    def failed_pages(self, book_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT page_number, last_error, retry_count FROM pages WHERE book_id=? AND status=?",
                (book_id, PageStatus.FAILED.value),
            ).fetchall()
            return [{"page": r[0], "error": r[1], "retry_count": r[2]} for r in rows]
