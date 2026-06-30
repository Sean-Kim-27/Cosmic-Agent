"""Provider-agnostic CGI memory schema and JSON validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class CGISchemaError(ValueError):
    """Raised when LLM-produced CGI JSON cannot be normalized."""


class CGINodeDraft(BaseModel):
    """A memory node proposed by the background CGI parser."""

    label: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=2_000)
    kind: str = Field(default="memory", min_length=1, max_length=80)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list, max_length=24)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    @field_validator("label", "summary", "kind")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = value.strip().lower()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag[:80])
        return normalized


class CGIEdgeDraft(BaseModel):
    """A relationship proposed between two CGI memory nodes."""

    source_label: str = Field(min_length=1, max_length=160)
    target_label: str = Field(min_length=1, max_length=160)
    relation: str = Field(default="related", min_length=1, max_length=120)
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    @field_validator("source_label", "target_label", "relation")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized


class CGIMemoryDocument(BaseModel):
    """Normalized memory graph fragment extracted from a completed answer."""

    nodes: list[CGINodeDraft] = Field(default_factory=list, max_length=200)
    edges: list[CGIEdgeDraft] = Field(default_factory=list, max_length=400)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    def limited(self, max_nodes: int) -> CGIMemoryDocument:
        """Return a copy trimmed to the configured node budget."""

        node_labels = {node.label for node in self.nodes[:max_nodes]}
        return CGIMemoryDocument(
            nodes=self.nodes[:max_nodes],
            edges=[
                edge
                for edge in self.edges
                if edge.source_label in node_labels and edge.target_label in node_labels
            ],
            metadata=self.metadata,
        )


def parse_cgi_memory_document(payload: Mapping[str, Any] | str) -> CGIMemoryDocument:
    """Parse provider JSON into a validated CGI memory document."""

    try:
        if isinstance(payload, str):
            data = json.loads(payload)
        else:
            data = dict(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CGISchemaError("CGI parser returned invalid JSON") from exc

    try:
        return CGIMemoryDocument.model_validate(data)
    except ValidationError as exc:
        raise CGISchemaError("CGI parser returned an invalid memory schema") from exc


def cgi_memory_json_schema() -> dict[str, Any]:
    """Return the JSON schema requested from structured-output providers."""

    return CGIMemoryDocument.model_json_schema()
