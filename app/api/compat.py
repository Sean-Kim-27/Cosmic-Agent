"""Compatibility routes for the Phase 5.5 verification contract."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.cgi import get_cgi_tree
from app.api.chat import stream_chat
from app.api.schemas import CGITreeResponse, SettingsDashboardResponse, UsageSummaryResponse
from app.api.settings import get_settings_dashboard
from app.api.usage import get_usage_today

router = APIRouter(prefix="/api", tags=["compat"])

router.add_api_route(
    "/config",
    get_settings_dashboard,
    methods=["GET"],
    response_model=SettingsDashboardResponse,
    summary="Compatibility alias for /api/v1/settings",
)
router.add_api_route(
    "/memory/nodes",
    get_cgi_tree,
    methods=["GET"],
    response_model=CGITreeResponse,
    summary="Compatibility alias for /api/v1/cgi/tree",
)
router.add_api_route(
    "/chat/stream",
    stream_chat,
    methods=["POST"],
    summary="Compatibility alias for /api/v1/chat/stream",
)
router.add_api_route(
    "/usage/today",
    get_usage_today,
    methods=["GET"],
    response_model=UsageSummaryResponse,
    summary="Compatibility alias for /api/v1/usage/today",
)
