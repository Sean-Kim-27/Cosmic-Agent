"""Minimal API security middleware: shared-secret auth and IP rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_API_PREFIX = "/api/"
_API_PREFIX_EXACT = "/api"
_SECRET_HEADER = "x-cosmic-api-key"


@dataclass(slots=True)
class InMemoryIPRateLimiter:
    """Fixed-window-ish deque limiter scoped to one process."""

    limit_per_minute: int
    window_seconds: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, int, float]:
        current_time = time.monotonic() if now is None else now
        bucket = self._hits[key]
        cutoff = current_time - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit_per_minute:
            retry_after = max(1.0, self.window_seconds - (current_time - bucket[0]))
            return False, 0, retry_after
        bucket.append(current_time)
        return True, self.limit_per_minute - len(bucket), 0.0


class APISecurityMiddleware:
    """Apply lightweight API protection without adding runtime dependencies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        frontend_api_secret: SecretStr | None,
        rate_limit_enabled: bool,
        rate_limit_per_minute: int,
    ) -> None:
        self.app = app
        self.frontend_api_secret = frontend_api_secret
        self.rate_limit_enabled = rate_limit_enabled
        self.rate_limiter = InMemoryIPRateLimiter(rate_limit_per_minute)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[arg-type]
            return

        request = Request(scope, receive=receive)  # type: ignore[arg-type]
        if not _is_api_path(request.url.path) or request.method == "OPTIONS":
            await self.app(scope, receive, send)  # type: ignore[arg-type]
            return

        auth_response = self._authenticate(request)
        if auth_response is not None:
            await auth_response(scope, receive, send)  # type: ignore[arg-type]
            return

        if self.rate_limit_enabled:
            allowed, remaining, retry_after = self.rate_limiter.check(_client_key(request))
            if not allowed:
                response = JSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": str(int(retry_after))},
                )
                await response(scope, receive, send)  # type: ignore[arg-type]
                return
            scope.setdefault("state", {})
            request.state.rate_limit_remaining = remaining

        await self.app(scope, receive, send)  # type: ignore[arg-type]

    def _authenticate(self, request: Request) -> Response | None:
        if self.frontend_api_secret is None:
            return None
        expected = self.frontend_api_secret.get_secret_value()
        if not expected:
            return None
        supplied = request.headers.get(_SECRET_HEADER)
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if supplied == expected:
            return None
        return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)


def _is_api_path(path: str) -> bool:
    return path == _API_PREFIX_EXACT or path.startswith(_API_PREFIX)


def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host
