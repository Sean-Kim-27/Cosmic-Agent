"""SQLite-backed chat history used by the dashboard and session restore APIs."""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ChatHistoryRole = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatHistoryMessageWrite:
    """A user-visible chat message to persist for session restore."""

    session_id: str
    role: ChatHistoryRole
    content: str
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ChatHistoryMessage:
    """One persisted chat history message."""

    id: str
    session_id: str
    role: ChatHistoryRole
    content: str
    provider: str | None
    model: str | None
    created_at: str


class SQLiteChatHistoryStore:
    """Small connection-per-operation chat history store.

    The store intentionally lives in ``app.core`` so HTTP, CLI, and future
    interfaces can all share the same session persistence boundary.
    """

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

    def save_message(self, write: ChatHistoryMessageWrite) -> ChatHistoryMessage:
        session_id = write.session_id.strip()
        content = write.content.strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        if write.role not in {"user", "assistant"}:
            raise ValueError("chat history role must be user or assistant")
        if not content:
            raise ValueError("chat history content must not be empty")

        message_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_messages (
                    id,
                    session_id,
                    role,
                    content,
                    provider,
                    model,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    message_id,
                    session_id,
                    write.role,
                    content,
                    write.provider,
                    write.model,
                ),
            )
            row = connection.execute(
                """
                SELECT *
                FROM chat_messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Saved chat history message could not be reloaded")
        return self._message_from_row(row)

    def list_messages(self, session_id: str, *, limit: int = 100) -> list[ChatHistoryMessage]:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM (
                    SELECT rowid AS sort_rowid, *
                    FROM chat_messages
                    WHERE session_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, sort_rowid ASC
                """,
                (normalized_session_id, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def clear_session(self, session_id: str) -> int:
        """Delete one session's dashboard history and return the deleted count."""

        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_messages WHERE session_id = ?",
                (normalized_session_id,),
            )
        return int(cursor.rowcount)

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
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created_at
            ON chat_messages(session_id, created_at, id);
            """
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ChatHistoryMessage:
        role = str(row["role"])
        if role not in {"user", "assistant"}:
            raise ValueError(f"Stored chat history message has invalid role: {role}")
        return ChatHistoryMessage(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            role=role,
            content=str(row["content"]),
            provider=str(row["provider"]) if row["provider"] is not None else None,
            model=str(row["model"]) if row["model"] is not None else None,
            created_at=str(row["created_at"]),
        )
