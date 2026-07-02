"""Read-only dashboard summary endpoint.

Aggregates job queue health, 24-hour activity, and chat session activity for
the visualization page. Implementations stay inside ``app.core`` so we do not
reach into the persistence layer from the FastAPI route definition itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app.api.dependencies import (
    get_cgi_memory_store,
    get_chat_history_store,
)
from app.api.schemas import (
    DashboardHoursEntryResponse,
    DashboardJobStatusCountResponse,
    DashboardSessionActivityResponse,
    DashboardSummaryResponse,
)
from app.core import (
    SQLiteCGIMemoryStore,
    SQLiteChatHistoryStore,
)
from app.core.chat_history_store import ChatSessionSummary

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_DASHBOARD_STATUS_KEYS: tuple[
    "PENDING", "PROCESSING", "COMPLETED", "FAILED", "QUOTA_LOCKED"
] = (
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "QUOTA_LOCKED",
)


@router.get("/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
    chat_history_store: SQLiteChatHistoryStore = Depends(get_chat_history_store),
) -> DashboardSummaryResponse:
    """Build the dashboard visualization payload in one aggregate call."""

    status_counts_raw = await asyncio.to_thread(
        memory_store.count_parse_jobs_by_status
    )
    hourly_counts_raw = await asyncio.to_thread(
        memory_store.hourly_parse_jobs_last_24h
    )
    sessions_raw = await asyncio.to_thread(
        chat_history_store.list_sessions, limit=50
    )

    job_status_counts: list[DashboardJobStatusCountResponse] = [
        DashboardJobStatusCountResponse(status=key, count=int(status_counts_raw.get(key, 0)))
        for key in _DASHBOARD_STATUS_KEYS
    ]

    total_jobs = sum(item.count for item in job_status_counts)
    completed = next(
        (item.count for item in job_status_counts if item.status == "COMPLETED"), 0
    )
    success_rate_percent = (
        round(completed / total_jobs * 100, 2) if total_jobs else 0.0
    )

    hourly_jobs_last_24h = [
        DashboardHoursEntryResponse(hour_utc=index, count=int(count))
        for index, count in enumerate(hourly_counts_raw)
    ]

    top_sessions = sorted(sessions_raw, key=_session_message_count, reverse=True)[:10]
    top_sessions_by_message_count = [
        DashboardSessionActivityResponse(
            session_id=session.session_id,
            message_count=int(_session_message_count(session)),
        )
        for session in top_sessions
    ]

    generated_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")

    return DashboardSummaryResponse(
        job_status_counts=job_status_counts,
        total_jobs=total_jobs,
        success_rate_percent=success_rate_percent,
        hourly_jobs_last_24h=hourly_jobs_last_24h,
        top_sessions_by_message_count=top_sessions_by_message_count,
        generated_at=generated_at,
    )


def _session_message_count(session: ChatSessionSummary) -> int:
    return int(getattr(session, "message_count", 0))
