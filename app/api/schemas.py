"""Pydantic schemas for Phase 3 streaming chat APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agent import AgentChatRequest, ChatMessage


class ChatMessagePayload(BaseModel):
    """Prior conversation message accepted by the HTTP API."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message content must not be empty")
        return normalized


class ChatStreamRequest(BaseModel):
    """Request body for the SSE chat stream endpoint."""

    message: str = Field(min_length=1, max_length=100_000)
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=256)
    history: list[ChatMessagePayload] = Field(default_factory=list, max_length=100)
    session_id: str | None = Field(default=None, max_length=160)
    parse_cgi: bool = True

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be empty")
        return normalized

    @field_validator("provider", "model", "session_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def to_agent_request(self) -> AgentChatRequest:
        """Convert API transport schema into the provider-neutral agent request."""

        return AgentChatRequest(
            message=self.message,
            history=tuple(ChatMessage(message.role, message.content) for message in self.history),
            provider=self.provider,
            model=self.model,
        )


class SettingStatusResponse(BaseModel):
    """Dashboard-safe runtime setting status."""

    key: str
    source: str
    secret: bool
    configured: bool
    value: str | None


class ProviderStatusResponse(BaseModel):
    """Dashboard-safe provider availability."""

    name: str
    registered: bool
    configured: bool
    default: bool


class ModelRouteResponse(BaseModel):
    """A model alias route."""

    alias: str
    provider: str
    model: str


class SettingsDashboardResponse(BaseModel):
    """Current settings, provider status, and model route snapshot."""

    settings: list[SettingStatusResponse]
    providers: list[ProviderStatusResponse]
    model_routes: list[ModelRouteResponse]


class SettingOverrideRequest(BaseModel):
    """Persist one runtime override in SQLite."""

    value: str | int | None = Field(default=None)


class ModelRouteRequest(BaseModel):
    """Create or update a model alias route."""

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)

    @field_validator("provider", "model")
    @classmethod
    def strip_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized


class PersonaResponse(BaseModel):
    """Dashboard editable prompt/persona values."""

    system_prompt: str
    persona: str


class PersonaUpdateRequest(BaseModel):
    """Update system prompt and/or persona."""

    system_prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    persona: str | None = Field(default=None, min_length=1, max_length=100_000)

    @field_validator("system_prompt", "persona")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> PersonaUpdateRequest:
        if self.system_prompt is None and self.persona is None:
            raise ValueError("At least one field must be provided")
        return self


class CGINodeResponse(BaseModel):
    """A CGI memory node returned to the dashboard."""

    id: str
    interaction_id: str
    label: str
    kind: str
    summary: str
    weight: float
    tags: list[str]
    metadata: dict[str, Any]
    created_at: str


class CGIEdgeResponse(BaseModel):
    """A CGI memory edge returned to the dashboard."""

    id: str
    interaction_id: str
    source_label: str
    target_label: str
    relation: str
    weight: float
    metadata: dict[str, Any]
    created_at: str


class CGIInteractionTreeResponse(BaseModel):
    """Tree root for one stored interaction."""

    id: str
    session_id: str | None
    user_message: str
    assistant_answer: str
    parser_provider: str
    parser_model: str
    created_at: str
    nodes: list[CGINodeResponse]
    edges: list[CGIEdgeResponse]


class CGITreeResponse(BaseModel):
    """Recent CGI memory tree grouped by interaction."""

    interactions: list[CGIInteractionTreeResponse]


class ChatHistoryMessageResponse(BaseModel):
    """A persisted chat message for dashboard session restore."""

    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    provider: str | None
    model: str | None
    created_at: str


class ChatHistoryResponse(BaseModel):
    """Session chat history plus the CGI graph context for that session."""

    session_id: str
    messages: list[ChatHistoryMessageResponse]
    cgi_context: CGITreeResponse


class CGIPruningEventResponse(BaseModel):
    """A pruning event emitted by the CGI memory maintenance boundary."""

    id: str
    strategy: str
    before_interactions: int
    after_interactions: int
    before_nodes: int
    after_nodes: int
    before_edges: int
    after_edges: int
    pruned_interactions: int
    pruned_nodes: int
    pruned_edges: int
    created_at: str


class CGIMemoryMaintenanceResponse(BaseModel):
    """Immediate pruning result for stress verification."""

    strategy: str
    before_interactions: int
    after_interactions: int
    before_nodes: int
    after_nodes: int
    before_edges: int
    after_edges: int
    pruned_interactions: int
    pruned_nodes: int
    pruned_edges: int


class UsageRecordResponse(BaseModel):
    """One recent LLM usage record."""

    id: str
    provider: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    metadata: dict[str, Any]
    created_at: str


class UsageSummaryResponse(BaseModel):
    """Dashboard summary for today's token usage."""

    since: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    records: list[UsageRecordResponse] = Field(default_factory=list)


class CGIParseJobResponse(BaseModel):
    """One persisted background CGI parse job."""

    id: str
    session_id: str | None
    user_message: str
    assistant_answer: str
    status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "QUOTA_LOCKED"]
    attempts: int
    max_attempts: int
    last_error_type: str | None
    last_error_message: str | None
    next_run_at: str | None
    locked_at: str | None
    completed_at: str | None
    interaction_id: str | None
    created_at: str
    updated_at: str


class JobRetryRequest(BaseModel):
    """Retry stopped background jobs from dashboard controls."""

    statuses: list[Literal["FAILED", "QUOTA_LOCKED"]] = Field(
        default_factory=lambda: ["FAILED", "QUOTA_LOCKED"],
        max_length=2,
    )
    process_limit: int = Field(default=3, ge=1, le=50)


class JobRetryResponse(BaseModel):
    """Retry-all result after stopped jobs were requeued."""

    reset_count: int
    statuses: list[str]
    processing_scheduled: bool


class CGINodePatchRequest(BaseModel):
    """Partial CGI node edit request."""

    label: str | None = Field(default=None, min_length=1, max_length=160)
    kind: str | None = Field(default=None, min_length=1, max_length=80)
    summary: str | None = Field(default=None, min_length=1, max_length=2_000)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    tags: list[str] | None = Field(default=None, max_length=24)
    metadata: dict[str, Any] | None = None

    @field_validator("label", "kind", "summary")
    @classmethod
    def strip_optional_node_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = value.strip().lower()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag[:80])
        return normalized

    @model_validator(mode="after")
    def require_at_least_one_patch_field(self) -> CGINodePatchRequest:
        if (
            self.label is None
            and self.kind is None
            and self.summary is None
            and self.weight is None
            and self.tags is None
            and self.metadata is None
        ):
            raise ValueError("At least one field must be provided")
        return self


class DataResponse(BaseModel):
    """Simple JSON API envelope."""

    data: Any
