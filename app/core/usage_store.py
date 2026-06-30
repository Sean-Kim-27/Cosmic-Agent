"""SQLite-backed LLM usage and token cost accounting."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMUsageWrite:
    """One tracked LLM call."""

    provider: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class LLMUsageRecord:
    """A stored LLM usage row."""

    id: str
    provider: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class LLMUsageSummary:
    """Aggregated LLM usage for dashboard cost cards."""

    since: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class SQLiteUsageStore:
    """Connection-per-operation store for LLM usage accounting."""

    def __init__(self, path: Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = path.expanduser()
        self.timeout_seconds = timeout_seconds
        self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._enable_wal_if_available(connection)
            self._apply_schema(connection)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save(self, write: LLMUsageWrite) -> LLMUsageRecord:
        """Persist one usage event and return the stored record."""

        record_id = uuid.uuid4().hex
        total_tokens = write.prompt_tokens + write.completion_tokens
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_usage (
                    id,
                    provider,
                    model,
                    operation,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    record_id,
                    write.provider,
                    write.model,
                    write.operation,
                    write.prompt_tokens,
                    write.completion_tokens,
                    total_tokens,
                    write.estimated_cost_usd,
                    json.dumps(write.metadata, ensure_ascii=False),
                ),
            )
        record = self.get(record_id)
        if record is None:
            raise RuntimeError("Saved LLM usage record could not be reloaded")
        return record

    def get(self, record_id: str) -> LLMUsageRecord | None:
        """Return one usage record by id."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    provider,
                    model,
                    operation,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    metadata_json,
                    created_at
                FROM llm_usage
                WHERE id = ?
                """,
                (record_id,),
            ).fetchone()
        return None if row is None else self._record_from_row(row)

    def list_recent(self, *, limit: int = 100) -> list[LLMUsageRecord]:
        """List recent usage events newest-first."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    provider,
                    model,
                    operation,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    metadata_json,
                    created_at
                FROM llm_usage
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def summarize_today(self) -> LLMUsageSummary:
        """Return usage aggregated for the local SQLite day."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    DATE('now', 'localtime') AS since,
                    COUNT(*) AS calls,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd
                FROM llm_usage
                WHERE DATE(created_at, 'localtime') = DATE('now', 'localtime')
                """
            ).fetchone()
        return LLMUsageSummary(
            since=str(row["since"]),
            calls=int(row["calls"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            total_tokens=int(row["total_tokens"]),
            estimated_cost_usd=float(row["estimated_cost_usd"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _enable_wal_if_available(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise

    @staticmethod
    def _apply_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                operation TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at
            ON llm_usage(created_at);
            """
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> LLMUsageRecord:
        return LLMUsageRecord(
            id=str(row["id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            operation=str(row["operation"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            total_tokens=int(row["total_tokens"]),
            estimated_cost_usd=float(row["estimated_cost_usd"]),
            metadata=_decode_json_object(str(row["metadata_json"])),
            created_at=str(row["created_at"]),
        )


def estimate_text_tokens(text: str) -> int:
    """Return a conservative local token estimate when provider usage is unavailable."""

    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def estimate_usage_cost(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> float:
    """Estimate USD cost from token counts and configured per-million rates."""

    return round(
        (prompt_tokens / 1_000_000) * input_cost_per_million
        + (completion_tokens / 1_000_000) * output_cost_per_million,
        8,
    )


def _decode_json_object(payload: str) -> dict[str, object]:
    value: Any = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Stored usage metadata payload is invalid")
    return dict(value)
