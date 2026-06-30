"""FastAPI application factory for the Cosmic Agent API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.cgi import router as cgi_router
from app.api.chat import router as chat_router
from app.api.compat import router as compat_router
from app.api.dependencies import get_cgi_background_parser, get_mcp_client
from app.api.jobs import router as jobs_router
from app.api.security import APISecurityMiddleware
from app.api.settings import router as settings_router
from app.api.usage import router as usage_router
from app.config import load_settings

logger = logging.getLogger("uvicorn.error")


def create_app(*, start_background_workers: bool = False) -> FastAPI:
    lifespan = _worker_lifespan if start_background_workers else None
    settings = load_settings()
    app = FastAPI(title="Cosmic Agent API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        APISecurityMiddleware,
        frontend_api_secret=settings.frontend_api_secret,
        rate_limit_enabled=settings.api_rate_limit_enabled,
        rate_limit_per_minute=settings.api_rate_limit_per_minute,
    )
    app.include_router(chat_router)
    app.include_router(settings_router)
    app.include_router(cgi_router)
    app.include_router(jobs_router)
    app.include_router(usage_router)
    app.include_router(compat_router)
    return app


@asynccontextmanager
async def _worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app
    parser = get_cgi_background_parser()
    mcp_client = get_mcp_client()
    recovery_task = asyncio.create_task(parser.run_stale_recovery_loop())
    logger.info("Cosmic Agent background workers started")
    try:
        yield
    finally:
        recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_task
        if mcp_client is not None:
            await mcp_client.close()
        logger.info("Cosmic Agent background workers stopped")


app = create_app(start_background_workers=True)
