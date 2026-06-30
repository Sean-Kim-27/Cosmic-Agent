"""Small Server-Sent Events encoder."""

from __future__ import annotations

import json
import re
from typing import Any

_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def encode_sse(event: str, data: Any) -> bytes:
    """Encode one SSE event with JSON data and safe constant event names."""

    if not _EVENT_NAME_PATTERN.fullmatch(event):
        raise ValueError("Invalid SSE event name")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event}"]
    lines.extend(f"data: {line}" for line in payload.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode("utf-8")
