"""Dashboard LLM token and estimated cost APIs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_usage_store
from app.api.schemas import UsageRecordResponse, UsageSummaryResponse
from app.core import SQLiteUsageStore

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


@router.get("/today", response_model=UsageSummaryResponse)
async def get_usage_today(
    limit: int = Query(default=20, ge=0, le=200),
    usage_store: SQLiteUsageStore = Depends(get_usage_store),
) -> UsageSummaryResponse:
    """Return today's token counts, estimated cost, and recent call records."""

    summary, records = await asyncio.gather(
        asyncio.to_thread(usage_store.summarize_today),
        asyncio.to_thread(usage_store.list_recent, limit=limit),
    )
    return UsageSummaryResponse(
        **asdict(summary),
        records=[UsageRecordResponse(**asdict(record)) for record in records],
    )
