"""Phase 3 SSE chat endpoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import APIRouter, BackgroundTasks, Depends
from starlette.responses import StreamingResponse

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIBackgroundParser,
    CGIParseJob,
    CosmicAgentService,
)
from app.agent.llm_provider import LLMProviderError
from app.agent.runtime import LLMRuntimeError
from app.api.dependencies import get_agent_service, get_cgi_background_parser
from app.api.schemas import ChatStreamRequest
from app.api.sse import encode_sse
from app.config import ConfigManagerError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])


@dataclass(slots=True)
class StreamBuffer:
    """Collect text for the post-stream background parse task."""

    chunks: list[str] = field(default_factory=list)
    completed: bool = False

    def append(self, text: str) -> None:
        self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@router.post("/chat/stream")
async def stream_chat(
    payload: ChatStreamRequest,
    background_tasks: BackgroundTasks,
    service: CosmicAgentService = Depends(get_agent_service),
    cgi_parser: CGIBackgroundParser = Depends(get_cgi_background_parser),
) -> StreamingResponse:
    """Stream LLM text immediately, then parse CGI memory after completion."""

    request = payload.to_agent_request()
    buffer = StreamBuffer()
    if payload.parse_cgi:
        background_tasks.add_task(
            _parse_completed_stream,
            cgi_parser,
            payload,
            buffer,
        )

    return StreamingResponse(
        _stream_sse(service, request, buffer, parse_cgi=payload.parse_cgi),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        background=background_tasks,
    )


async def _stream_sse(
    service: CosmicAgentService,
    request: AgentChatRequest,
    buffer: StreamBuffer,
    *,
    parse_cgi: bool,
) -> AsyncIterator[bytes]:
    try:
        async for event in service.stream_reply_events(request):
            if isinstance(event, AgentStreamStarted):
                yield encode_sse(
                    "metadata",
                    {
                        "provider": event.provider,
                        "model": event.model,
                    },
                )
            elif isinstance(event, AgentTextDelta):
                buffer.append(event.text)
                yield encode_sse("token", {"text": event.text})
            elif isinstance(event, AgentStreamCompleted):
                buffer.completed = True
                yield encode_sse(
                    "done",
                    {
                        "provider": event.provider,
                        "model": event.model,
                        "parse_cgi": parse_cgi,
                    },
                )
    except (ConfigManagerError, LLMProviderError, LLMRuntimeError, ValueError) as exc:
        yield encode_sse(
            "error",
            {
                "code": "stream_error",
                "message": str(exc),
            },
        )
    except Exception:
        logger.exception("Unexpected streaming failure")
        yield encode_sse(
            "error",
            {
                "code": "internal_stream_error",
                "message": "Unexpected streaming failure",
            },
        )


async def _parse_completed_stream(
    cgi_parser: CGIBackgroundParser,
    payload: ChatStreamRequest,
    buffer: StreamBuffer,
) -> None:
    if not buffer.completed:
        return
    answer = buffer.text.strip()
    if not answer:
        return
    await cgi_parser.parse_and_store_safely(
        CGIParseJob(
            session_id=payload.session_id,
            user_message=payload.message,
            assistant_answer=answer,
        )
    )
