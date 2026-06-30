"""Dashboard APIs for background CGI parse job monitoring and retry."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.agent import CGIBackgroundParser
from app.api.dependencies import get_cgi_background_parser, get_cgi_memory_store
from app.api.schemas import CGIParseJobResponse, JobRetryRequest, JobRetryResponse
from app.core import CGIParseJobStatus, SQLiteCGIMemoryStore

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=list[CGIParseJobResponse])
async def list_jobs(
    status: list[CGIParseJobStatus] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> list[CGIParseJobResponse]:
    """List background CGI parse jobs newest-first."""

    statuses = tuple(status) if status else None
    jobs = await asyncio.to_thread(
        memory_store.list_parse_jobs,
        statuses=statuses,
        limit=limit,
    )
    return [CGIParseJobResponse(**asdict(job)) for job in jobs]


@router.post("/retry", response_model=JobRetryResponse)
async def retry_jobs(
    background_tasks: BackgroundTasks,
    payload: JobRetryRequest | None = None,
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
    cgi_parser: CGIBackgroundParser = Depends(get_cgi_background_parser),
) -> JobRetryResponse:
    """Requeue FAILED/QUOTA_LOCKED jobs and schedule worker processing."""

    request = payload or JobRetryRequest()
    statuses: tuple[Literal["FAILED", "QUOTA_LOCKED"], ...] = tuple(request.statuses)
    reset_count = await asyncio.to_thread(
        memory_store.reset_parse_jobs_for_retry,
        statuses=statuses,
    )
    processing_scheduled = reset_count > 0
    if processing_scheduled:
        background_tasks.add_task(cgi_parser.process_due_jobs, limit=request.process_limit)
    return JobRetryResponse(
        reset_count=reset_count,
        statuses=list(statuses),
        processing_scheduled=processing_scheduled,
    )
