from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.api.dependencies import get_cgi_background_parser, get_cgi_memory_store
from app.core import CGIParseJobWrite, SQLiteCGIMemoryStore


class FakeCGIParser:
    def __init__(self) -> None:
        self.process_calls: list[int] = []

    async def process_due_jobs(self, *, limit: int | None = None) -> list[object]:
        self.process_calls.append(limit or 0)
        return []


def build_memory_store(tmp_path: Path) -> SQLiteCGIMemoryStore:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    failed = store.enqueue_parse_job(
        CGIParseJobWrite(
            session_id="s1",
            user_message="failed user",
            assistant_answer="failed answer",
        )
    )
    quota = store.enqueue_parse_job(
        CGIParseJobWrite(
            session_id="s2",
            user_message="quota user",
            assistant_answer="quota answer",
        )
    )
    store.mark_parse_job_failed(
        failed.id,
        status="FAILED",
        error_type="http_503",
        error_message="temporarily unavailable",
    )
    store.mark_parse_job_failed(
        quota.id,
        status="QUOTA_LOCKED",
        error_type="quota_exceeded",
        error_message="quota exceeded",
    )
    return store


def test_jobs_api_lists_and_retries_stopped_jobs(tmp_path: Path) -> None:
    memory_store = build_memory_store(tmp_path)
    parser = FakeCGIParser()
    app = create_app()
    app.dependency_overrides[get_cgi_memory_store] = lambda: memory_store
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser

    with TestClient(app) as client:
        jobs = client.get("/api/v1/jobs")
        retry = client.post("/api/v1/jobs/retry", json={"process_limit": 7})
        retried_jobs = client.get("/api/v1/jobs", params={"status": "PENDING"})

    assert jobs.status_code == 200
    assert {job["status"] for job in jobs.json()} == {"FAILED", "QUOTA_LOCKED"}
    assert retry.status_code == 200
    assert retry.json() == {
        "reset_count": 2,
        "statuses": ["FAILED", "QUOTA_LOCKED"],
        "processing_scheduled": True,
    }
    assert parser.process_calls == [7]
    assert retried_jobs.status_code == 200
    assert {job["status"] for job in retried_jobs.json()} == {"PENDING"}
    assert {job["attempts"] for job in retried_jobs.json()} == {0}
