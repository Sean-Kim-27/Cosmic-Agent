from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.application import create_app


def test_frontend_api_secret_blocks_api_requests_without_header(monkeypatch) -> None:
    monkeypatch.setenv("FRONTEND_API_SECRET", "test-secret")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "100")
    app = create_app()

    @app.get("/api/test-auth")
    async def test_auth() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        missing = client.get("/api/test-auth")
        supplied = client.get("/api/test-auth", headers={"X-Cosmic-API-Key": "test-secret"})

    assert missing.status_code == 401
    assert supplied.status_code == 200
    assert supplied.json() == {"ok": True}


def test_ip_rate_limit_returns_429_after_configured_limit(monkeypatch) -> None:
    monkeypatch.setenv("FRONTEND_API_SECRET", "")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "2")
    app = create_app()

    @app.get("/api/test-rate")
    async def test_rate() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        first = client.get("/api/test-rate")
        second = client.get("/api/test-rate")
        third = client.get("/api/test-rate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["retry-after"]
