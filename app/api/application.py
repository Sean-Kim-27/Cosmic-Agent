"""FastAPI application factory for the Cosmic Agent API."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.cgi import router as cgi_router
from app.api.chat import router as chat_router
from app.api.settings import router as settings_router


def create_app() -> FastAPI:
    app = FastAPI(title="Cosmic Agent API", version="0.1.0")
    app.include_router(chat_router)
    app.include_router(settings_router)
    app.include_router(cgi_router)
    return app


app = create_app()
