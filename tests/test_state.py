from bookocr.core.state import StateStore
from bookocr.core.types import PageStatus


def test_resume_semantics(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.register_book("book1", "/fake/path.pdf", page_count=5, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v1")

    assert store.pending_pages("book1") == [1, 2, 3, 4, 5]

    store.set_page_status("book1", 1, PageStatus.COMPLETED, quality_tier="HIGH", confidence=90.0)
    store.set_page_status("book1", 2, PageStatus.PROCESSING)

    # A page stuck mid-flight (PROCESSING, e.g. from a crash) must be
    # re-queued on resume, not skipped.
    assert store.pending_pages("book1") == [2, 3, 4, 5]

    store.set_page_status("book1", 3, PageStatus.FAILED, error="boom", bump_retry=True)
    failed = store.failed_pages("book1")
    assert failed == [{"page": 3, "error": "boom", "retry_count": 1}]

    progress = store.book_progress("book1")
    assert progress["total_pages"] == 5
    assert progress["by_status"]["COMPLETED"] == 1


def test_register_book_is_idempotent(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.register_book("book1", "/fake/path.pdf", page_count=3, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v1")
    store.set_page_status("book1", 1, PageStatus.COMPLETED)

    # Re-registering (e.g. a second `process` invocation) must not wipe
    # already-recorded progress.
    store.register_book("book1", "/fake/path.pdf", page_count=3, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v1")
    assert store.pending_pages("book1") == [2, 3]


def test_register_book_rejects_different_source_or_revision(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.register_book("book1", "/first.pdf", page_count=3, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v1")

    import pytest

    with pytest.raises(ValueError, match="different PDF"):
        store.register_book("book1", "/second.pdf", page_count=3, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v1")
    with pytest.raises(ValueError, match="source PDF has changed"):
        store.register_book("book1", "/first.pdf", page_count=3, pipeline_version="0.1.0", config_hash="abc", source_fingerprint="v2")
