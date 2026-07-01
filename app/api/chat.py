"""Phase 3 SSE chat endpoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import APIRouter, BackgroundTasks, Depends, Query
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
from app.api.dependencies import (
    get_agent_service,
    get_cgi_background_parser,
    get_cgi_memory_store,
    get_chat_history_store,
)
from app.api.schemas import (
    CGIEdgeResponse,
    CGIInteractionTreeResponse,
    CGINodeResponse,
    CGITreeResponse,
    ChatHistoryMessageResponse,
    ChatHistoryResponse,
    ChatSessionClearResponse,
    ChatSessionListResponse,
    ChatSessionSummaryResponse,
    ChatStreamRequest,
)
from app.api.sse import encode_sse
from app.config import ConfigManagerError
from app.core import (
    ChatHistoryMessageWrite,
    SQLiteCGIMemoryStore,
    SQLiteChatHistoryStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])


@dataclass(slots=True)
class StreamBuffer:
    """Collect text for the post-stream background parse task."""

    chunks: list[str] = field(default_factory=list)
    completed: bool = False
    provider: str | None = None
    model: str | None = None

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
    history_store: SQLiteChatHistoryStore = Depends(get_chat_history_store),
) -> StreamingResponse:
    """Stream LLM text immediately, then parse CGI memory after completion."""

    request = payload.to_agent_request()
    buffer = StreamBuffer()
    if payload.session_id is not None:
        background_tasks.add_task(
            _persist_completed_chat,
            history_store,
            payload,
            buffer,
        )
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
                buffer.provider = event.provider
                buffer.model = event.model
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
    await cgi_parser.enqueue_and_process_safely(
        CGIParseJob(
            session_id=payload.session_id,
            user_message=payload.message,
            assistant_answer=answer,
        )
    )


async def _persist_completed_chat(
    history_store: SQLiteChatHistoryStore,
    payload: ChatStreamRequest,
    buffer: StreamBuffer,
) -> None:
    if not buffer.completed or payload.session_id is None:
        return
    answer = buffer.text.strip()
    if not answer:
        return
    await asyncio.to_thread(
        history_store.save_message,
        ChatHistoryMessageWrite(
            session_id=payload.session_id,
            role="user",
            content=payload.message,
        ),
    )
    await asyncio.to_thread(
        history_store.save_message,
        ChatHistoryMessageWrite(
            session_id=payload.session_id,
            role="assistant",
            content=answer,
            provider=buffer.provider,
            model=buffer.model,
        ),
    )


@router.get("/chat/sessions", response_model=ChatSessionListResponse)
async def list_chat_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    history_store: SQLiteChatHistoryStore = Depends(get_chat_history_store),
) -> ChatSessionListResponse:
    """Return recent persisted dashboard chat sessions for the session switcher."""

    sessions = await asyncio.to_thread(history_store.list_sessions, limit=limit)
    return ChatSessionListResponse(
        sessions=[
            ChatSessionSummaryResponse(
                session_id=session.session_id,
                message_count=session.message_count,
                preview=session.preview,
                provider=session.provider,
                model=session.model,
                updated_at=session.updated_at,
            )
            for session in sessions
        ]
    )


@router.get("/chat/history/{session_id}", response_model=ChatHistoryResponse)
async def get_chat_history(
    session_id: str,
    limit_messages: int = Query(default=100, ge=1, le=500),
    limit_interactions: int = Query(default=50, ge=1, le=200),
    history_store: SQLiteChatHistoryStore = Depends(get_chat_history_store),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> ChatHistoryResponse:
    """Return persisted chat history and CGI memory context for one session."""

    messages = await asyncio.to_thread(
        history_store.list_messages,
        session_id,
        limit=limit_messages,
    )
    tree = await asyncio.to_thread(
        memory_store.get_tree,
        limit_interactions=limit_interactions,
        session_id=session_id,
    )
    return ChatHistoryResponse(
        session_id=session_id,
        messages=[
            ChatHistoryMessageResponse(
                id=message.id,
                session_id=message.session_id,
                role=message.role,
                content=message.content,
                provider=message.provider,
                model=message.model,
                created_at=message.created_at,
            )
            for message in messages
        ],
        cgi_context=CGITreeResponse(
            interactions=[
                CGIInteractionTreeResponse(
                    id=interaction.id,
                    session_id=interaction.session_id,
                    user_message=interaction.user_message,
                    assistant_answer=interaction.assistant_answer,
                    parser_provider=interaction.parser_provider,
                    parser_model=interaction.parser_model,
                    created_at=interaction.created_at,
                    nodes=[
                        CGINodeResponse(
                            id=node.id,
                            interaction_id=node.interaction_id,
                            label=node.label,
                            kind=node.kind,
                            summary=node.summary,
                            weight=node.weight,
                            tags=list(node.tags),
                            metadata=dict(node.metadata),
                            created_at=node.created_at,
                        )
                        for node in interaction.nodes
                    ],
                    edges=[
                        CGIEdgeResponse(
                            id=edge.id,
                            interaction_id=edge.interaction_id,
                            source_label=edge.source_label,
                            target_label=edge.target_label,
                            relation=edge.relation,
                            weight=edge.weight,
                            metadata=dict(edge.metadata),
                            created_at=edge.created_at,
                        )
                        for edge in interaction.edges
                    ],
                )
                for interaction in tree.interactions
            ]
        ),
    )


@router.delete("/chat/history/{session_id}", response_model=ChatSessionClearResponse)
async def clear_chat_history(
    session_id: str,
    history_store: SQLiteChatHistoryStore = Depends(get_chat_history_store),
) -> ChatSessionClearResponse:
    """Clear one dashboard chat session's persisted message history."""

    deleted = await asyncio.to_thread(history_store.clear_session, session_id)
    return ChatSessionClearResponse(session_id=session_id, deleted_messages=deleted)
