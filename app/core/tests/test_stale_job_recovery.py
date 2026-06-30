from __future__ import annotations

from pathlib import Path

from app.core import CGIParseJobWrite, SQLiteCGIMemoryStore


def make_stale_processing_job(
    store: SQLiteCGIMemoryStore,
    *,
    max_attempts: int,
    locked_minutes_ago: int = 20,
) -> str:
    queued = store.enqueue_parse_job(
        CGIParseJobWrite(
            session_id="stale-session",
            user_message="stale user message",
            assistant_answer="stale assistant answer",
            max_attempts=max_attempts,
        )
    )
    claimed = store.claim_next_parse_job()
    assert claimed is not None
    assert claimed.id == queued.id
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE cgi_parse_jobs
            SET locked_at = DATETIME(CURRENT_TIMESTAMP, ?)
            WHERE id = ?
            """,
            (f"-{locked_minutes_ago} minutes", queued.id),
        )
    return queued.id


def test_stale_processing_job_is_requeued_when_retry_budget_remains(tmp_path: Path) -> None:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    job_id = make_stale_processing_job(store, max_attempts=3)

    result = store.recover_stale_parse_jobs(stale_after_seconds=600, limit=10)
    recovered = store.get_parse_job(job_id)

    assert result.scanned == 1
    assert result.requeued == 1
    assert result.failed == 0
    assert result.job_ids == (job_id,)
    assert recovered is not None
    assert recovered.status == "PENDING"
    assert recovered.attempts == 1
    assert recovered.locked_at is None
    assert recovered.last_error_type == "stale_lock_recovered"


def test_stale_processing_job_fails_when_retry_budget_is_exhausted(tmp_path: Path) -> None:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    job_id = make_stale_processing_job(store, max_attempts=1)

    result = store.recover_stale_parse_jobs(stale_after_seconds=600, limit=10)
    recovered = store.get_parse_job(job_id)

    assert result.scanned == 1
    assert result.requeued == 0
    assert result.failed == 1
    assert result.job_ids == (job_id,)
    assert recovered is not None
    assert recovered.status == "FAILED"
    assert recovered.attempts == 1
    assert recovered.locked_at is None
    assert recovered.last_error_type == "stale_lock_exhausted"
