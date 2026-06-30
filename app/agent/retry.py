"""Retry classification for background LLM jobs.

The CGI worker must not blindly retry every provider failure. Temporary rate
limits and transport hiccups can recover with backoff, but quota exhaustion is a
durable stop condition until the provider resets quota or a human changes keys.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

RetryKind = Literal["transient", "quota", "permanent"]

_RETRY_DELAY_PATTERN = re.compile(
    r"(?:retryDelay|retry_after|retry-after|retry in)[^0-9]*(?P<seconds>\d+(?:\.\d+)?)s?",
    re.IGNORECASE,
)
_QUOTA_TERMS = (
    "quota exceeded",
    "quotaexceeded",
    "free_tier",
    "free tier",
    "quotametric",
    "generativelanguage.googleapis.com/generate_content_free_tier",
    "insufficient quota",
    "billing hard limit",
    "resource_exhausted",
)
_RATE_LIMIT_TERMS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "deadline exceeded",
    "connection reset",
    "server overloaded",
    "try again",
    "retry",
)
_PERMANENT_TERMS = (
    "api key not valid",
    "invalid api key",
    "authentication",
    "unauthorized",
    "permission denied",
    "forbidden",
    "not found",
    "invalid_request",
)


@dataclass(frozen=True, slots=True)
class RetryClassification:
    """Structured retry decision for a provider exception."""

    kind: RetryKind
    error_type: str
    message: str
    retry_after_seconds: float | None = None


def classify_provider_exception(exc: BaseException) -> RetryClassification:
    """Classify provider errors without importing provider-specific SDK types."""

    message = _error_message(exc)
    status_code = _status_code(exc)
    retry_after = _retry_after_seconds(exc, message)
    normalized = message.lower()

    if status_code == 429 and _contains_any(normalized, _QUOTA_TERMS):
        return RetryClassification(
            kind="quota",
            error_type="quota_exceeded",
            message=message,
        )
    if status_code == 429:
        return RetryClassification(
            kind="transient",
            error_type="rate_limit",
            message=message,
            retry_after_seconds=retry_after,
        )
    if status_code in {408, 409, 425, 500, 502, 503, 504}:
        return RetryClassification(
            kind="transient",
            error_type=f"http_{status_code}",
            message=message,
            retry_after_seconds=retry_after,
        )
    if status_code in {400, 401, 403, 404} or _contains_any(normalized, _PERMANENT_TERMS):
        return RetryClassification(
            kind="permanent",
            error_type=f"http_{status_code}" if status_code is not None else "permanent_error",
            message=message,
        )
    if _contains_any(normalized, _QUOTA_TERMS):
        return RetryClassification(kind="quota", error_type="quota_exceeded", message=message)
    if _contains_any(normalized, _RATE_LIMIT_TERMS):
        return RetryClassification(
            kind="transient",
            error_type="transient_provider_error",
            message=message,
            retry_after_seconds=retry_after,
        )
    return RetryClassification(kind="permanent", error_type=type(exc).__name__, message=message)


def exponential_backoff_seconds(
    *,
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    provider_retry_after_seconds: float | None = None,
) -> float:
    """Return capped exponential backoff, respecting provider retry-after hints."""

    safe_attempt = max(1, attempt)
    calculated = base_seconds * (2 ** (safe_attempt - 1))
    if provider_retry_after_seconds is not None:
        calculated = max(calculated, provider_retry_after_seconds)
    return min(max_seconds, max(0.0, calculated))


def _error_message(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    response = getattr(exc, "response", None)
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str) and response_text.strip():
        return response_text.strip()
    return type(exc).__name__


def _status_code(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def _retry_after_seconds(exc: BaseException, message: str) -> float | None:
    headers = getattr(exc, "headers", None) or getattr(
        getattr(exc, "response", None),
        "headers",
        None,
    )
    if headers is not None:
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
            try:
                value = headers.get(key)
            except AttributeError:
                value = None
            parsed = _parse_positive_seconds(value)
            if parsed is not None:
                return parsed

    match = _RETRY_DELAY_PATTERN.search(message)
    if match:
        return _parse_positive_seconds(match.group("seconds"))
    return None


def _parse_positive_seconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip().rstrip("s"))
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)
