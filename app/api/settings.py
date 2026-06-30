"""Dashboard configuration and provider status APIs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status

from app.agent import LLMProviderFactory
from app.api.dependencies import get_config_manager, get_provider_factory
from app.api.schemas import (
    ModelRouteRequest,
    ModelRouteResponse,
    PersonaResponse,
    PersonaUpdateRequest,
    ProviderStatusResponse,
    SettingOverrideRequest,
    SettingsDashboardResponse,
    SettingStatusResponse,
)
from app.config import (
    ConfigManager,
    ConfigManagerError,
    InvalidConfigValueError,
    SecretOverrideUnavailableError,
    UnknownSettingError,
)

router = APIRouter(prefix="/api/v1", tags=["settings"])


@router.get("/settings", response_model=SettingsDashboardResponse)
async def get_settings_dashboard(
    config_manager: ConfigManager = Depends(get_config_manager),
    provider_factory: LLMProviderFactory = Depends(get_provider_factory),
) -> SettingsDashboardResponse:
    """Return dashboard-safe settings, provider statuses, and model aliases."""

    try:
        setting_statuses, provider_statuses, model_routes = await asyncio.gather(
            asyncio.to_thread(config_manager.list_statuses),
            asyncio.to_thread(provider_factory.list_statuses),
            asyncio.to_thread(config_manager.list_model_routes),
        )
    except ConfigManagerError as exc:
        raise _config_http_error(exc) from exc

    return SettingsDashboardResponse(
        settings=[SettingStatusResponse(**asdict(setting)) for setting in setting_statuses],
        providers=[ProviderStatusResponse(**asdict(provider)) for provider in provider_statuses],
        model_routes=[ModelRouteResponse(**asdict(route)) for route in model_routes],
    )


@router.put("/settings/{key}", response_model=list[SettingStatusResponse])
async def set_setting_override(
    key: str,
    payload: SettingOverrideRequest,
    config_manager: ConfigManager = Depends(get_config_manager),
) -> list[SettingStatusResponse]:
    """Set one runtime override and return the new safe setting snapshot."""

    try:
        await asyncio.to_thread(config_manager.set_override, key, payload.value)
        statuses = await asyncio.to_thread(config_manager.list_statuses)
    except ConfigManagerError as exc:
        raise _config_http_error(exc) from exc
    return [SettingStatusResponse(**asdict(setting)) for setting in statuses]


@router.delete("/settings/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_setting_override(
    key: str,
    config_manager: ConfigManager = Depends(get_config_manager),
) -> None:
    """Delete one runtime override; environment/default values become effective again."""

    try:
        await asyncio.to_thread(config_manager.delete_override, key)
    except ConfigManagerError as exc:
        raise _config_http_error(exc) from exc


@router.get("/model-routes", response_model=list[ModelRouteResponse])
async def list_model_routes(
    config_manager: ConfigManager = Depends(get_config_manager),
) -> list[ModelRouteResponse]:
    routes = await asyncio.to_thread(config_manager.list_model_routes)
    return [ModelRouteResponse(**asdict(route)) for route in routes]


@router.put("/model-routes/{alias}", response_model=ModelRouteResponse)
async def set_model_route(
    alias: str,
    payload: ModelRouteRequest,
    config_manager: ConfigManager = Depends(get_config_manager),
) -> ModelRouteResponse:
    try:
        route = await asyncio.to_thread(
            config_manager.set_model_route,
            alias,
            payload.provider,
            payload.model,
        )
    except ConfigManagerError as exc:
        raise _config_http_error(exc) from exc
    return ModelRouteResponse(**asdict(route))


@router.delete("/model-routes/{alias}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_route(
    alias: str,
    config_manager: ConfigManager = Depends(get_config_manager),
) -> None:
    await asyncio.to_thread(config_manager.delete_model_route, alias)


@router.get("/persona", response_model=PersonaResponse)
async def get_persona(
    config_manager: ConfigManager = Depends(get_config_manager),
) -> PersonaResponse:
    settings = await asyncio.to_thread(config_manager.get_effective_settings)
    return PersonaResponse(
        system_prompt=settings.system_prompt,
        persona=settings.persona,
    )


@router.put("/persona", response_model=PersonaResponse)
async def update_persona(
    payload: PersonaUpdateRequest,
    config_manager: ConfigManager = Depends(get_config_manager),
) -> PersonaResponse:
    try:
        if payload.system_prompt is not None:
            await asyncio.to_thread(
                config_manager.set_override,
                "system_prompt",
                payload.system_prompt,
            )
        if payload.persona is not None:
            await asyncio.to_thread(config_manager.set_override, "persona", payload.persona)
        settings = await asyncio.to_thread(config_manager.get_effective_settings)
    except ConfigManagerError as exc:
        raise _config_http_error(exc) from exc
    return PersonaResponse(
        system_prompt=settings.system_prompt,
        persona=settings.persona,
    )


def _config_http_error(exc: ConfigManagerError) -> HTTPException:
    if isinstance(exc, UnknownSettingError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (InvalidConfigValueError, SecretOverrideUnavailableError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Configuration operation failed",
    )
