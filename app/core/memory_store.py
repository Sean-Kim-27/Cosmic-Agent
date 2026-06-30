"""SQLite-backed CGI memory write boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.cgi_schema import CGIMemoryDocument


@dataclass(frozen=True, slots=True)
class CGIMemoryRecord:
    """Stored CGI parse result metadata."""

    interaction_id: str
    node_count: int
    edge_count: int


@dataclass(frozen=True, slots=True)
class CGIMemoryWrite:
    """Completed assistant answer plus the parsed CGI document to persist."""

    session_id: str | None
    user_message: str
    assistant_answer: str
    parser_provider: str
    parser_model: str
    document: CGIMemoryDocument


@dataclass(frozen=True, slots=True)
class CGIMemoryNode:
    """Dashboard-visible CGI node."""

    id: str
    interaction_id: str
    label: str
    kind: str
    summary: str
    weight: float
    tags: tuple[str, ...]
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class CGIMemoryEdge:
    """Dashboard-visible CGI edge."""

    id: str
    interaction_id: str
    source_label: str
    target_label: str
    relation: str
    weight: float
    metadata: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class CGIMemoryInteraction:
    """A stored assistant response grouped with its CGI graph fragment."""

    id: str
    session_id: str | None
    user_message: str
    assistant_answer: str
    parser_provider: str
    parser_model: str
    created_at: str
    nodes: tuple[CGIMemoryNode, ...]
    edges: tuple[CGIMemoryEdge, ...]


@dataclass(frozen=True, slots=True)
class CGIMemoryTree:
    """JSON-tree-ready CGI memory snapshot."""

    interactions: tuple[CGIMemoryInteraction, ...]


@dataclass(frozen=True, slots=True)
class CGIMemoryNodePatch:
    """Partial node update from dashboard brain-surgery APIs."""

    label: str | None = None
    kind: str | None = None
    summary: str | None = None
    weight: float | None = None
    tags: tuple[str, ...] | None = None
    metadata: dict[str, object] | None = None


class SQLiteCGIMemoryStore:
    """Small connection-per-operation store for Phase 3 CGI parse results."""

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

    def save(self, write: CGIMemoryWrite) -> CGIMemoryRecord:
        interaction_id = uuid.uuid4().hex
        document = write.document
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO cgi_interactions (
                        id,
                        session_id,
                        user_message,
                        assistant_answer,
                        parser_provider,
                        parser_model,
                        raw_document_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        interaction_id,
                        write.session_id,
                        write.user_message,
                        write.assistant_answer,
                        write.parser_provider,
                        write.parser_model,
                        document.model_dump_json(),
                    ),
                )
                for node in document.nodes:
                    connection.execute(
                        """
                        INSERT INTO cgi_nodes (
                            id,
                            interaction_id,
                            label,
                            kind,
                            summary,
                            weight,
                            tags_json,
                            metadata_json,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            uuid.uuid4().hex,
                            interaction_id,
                            node.label,
                            node.kind,
                            node.summary,
                            node.weight,
                            json.dumps(node.tags, ensure_ascii=False),
                            json.dumps(node.metadata, ensure_ascii=False),
                        ),
                    )
                for edge in document.edges:
                    connection.execute(
                        """
                        INSERT INTO cgi_edges (
                            id,
                            interaction_id,
                            source_label,
                            target_label,
                            relation,
                            weight,
                            metadata_json,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            uuid.uuid4().hex,
                            interaction_id,
                            edge.source_label,
                            edge.target_label,
                            edge.relation,
                            edge.weight,
                            json.dumps(edge.metadata, ensure_ascii=False),
                        ),
                    )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        return CGIMemoryRecord(
            interaction_id=interaction_id,
            node_count=len(document.nodes),
            edge_count=len(document.edges),
        )

    def count_nodes(self) -> int:
        """Return stored node count for tests and later dashboard wiring."""

        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cgi_nodes").fetchone()
        return int(row[0])

    def list_nodes(self, *, limit: int = 100, offset: int = 0) -> list[CGIMemoryNode]:
        """List memory nodes newest-first for dashboard tables."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    interaction_id,
                    label,
                    kind,
                    summary,
                    weight,
                    tags_json,
                    metadata_json,
                    created_at
                FROM cgi_nodes
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def get_node(self, node_id: str) -> CGIMemoryNode | None:
        """Return one CGI node by id."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    interaction_id,
                    label,
                    kind,
                    summary,
                    weight,
                    tags_json,
                    metadata_json,
                    created_at
                FROM cgi_nodes
                WHERE id = ?
                """,
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return self._node_from_row(row)

    def update_node(self, node_id: str, patch: CGIMemoryNodePatch) -> CGIMemoryNode | None:
        """Patch a node and keep label-based edge references in sync."""

        existing = self.get_node(node_id)
        if existing is None:
            return None

        updated_label = patch.label if patch.label is not None else existing.label
        updated_kind = patch.kind if patch.kind is not None else existing.kind
        updated_summary = patch.summary if patch.summary is not None else existing.summary
        updated_weight = patch.weight if patch.weight is not None else existing.weight
        updated_tags = patch.tags if patch.tags is not None else existing.tags
        updated_metadata = patch.metadata if patch.metadata is not None else existing.metadata

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE cgi_nodes
                    SET
                        label = ?,
                        kind = ?,
                        summary = ?,
                        weight = ?,
                        tags_json = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        updated_label,
                        updated_kind,
                        updated_summary,
                        updated_weight,
                        json.dumps(list(updated_tags), ensure_ascii=False),
                        json.dumps(updated_metadata, ensure_ascii=False),
                        node_id,
                    ),
                )
                if updated_label != existing.label:
                    connection.execute(
                        """
                        UPDATE cgi_edges
                        SET source_label = ?
                        WHERE interaction_id = ? AND source_label = ?
                        """,
                        (updated_label, existing.interaction_id, existing.label),
                    )
                    connection.execute(
                        """
                        UPDATE cgi_edges
                        SET target_label = ?
                        WHERE interaction_id = ? AND target_label = ?
                        """,
                        (updated_label, existing.interaction_id, existing.label),
                    )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        return self.get_node(node_id)

    def delete_node(self, node_id: str) -> bool:
        """Delete a node and label-matched edges from the same interaction."""

        existing = self.get_node(node_id)
        if existing is None:
            return False

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM cgi_edges
                    WHERE interaction_id = ?
                      AND (source_label = ? OR target_label = ?)
                    """,
                    (existing.interaction_id, existing.label, existing.label),
                )
                cursor = connection.execute("DELETE FROM cgi_nodes WHERE id = ?", (node_id,))
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        return cursor.rowcount > 0

    def get_tree(self, *, limit_interactions: int = 50) -> CGIMemoryTree:
        """Return recent interactions grouped with nodes and edges as a JSON tree."""

        with self._connect() as connection:
            interaction_rows = connection.execute(
                """
                SELECT
                    id,
                    session_id,
                    user_message,
                    assistant_answer,
                    parser_provider,
                    parser_model,
                    created_at
                FROM cgi_interactions
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit_interactions,),
            ).fetchall()
            interaction_ids = [str(row["id"]) for row in interaction_rows]
            if not interaction_ids:
                return CGIMemoryTree(interactions=())

            placeholders = ",".join("?" for _ in interaction_ids)
            node_rows = connection.execute(
                f"""
                SELECT
                    id,
                    interaction_id,
                    label,
                    kind,
                    summary,
                    weight,
                    tags_json,
                    metadata_json,
                    created_at
                FROM cgi_nodes
                WHERE interaction_id IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                tuple(interaction_ids),
            ).fetchall()
            edge_rows = connection.execute(
                f"""
                SELECT
                    id,
                    interaction_id,
                    source_label,
                    target_label,
                    relation,
                    weight,
                    metadata_json,
                    created_at
                FROM cgi_edges
                WHERE interaction_id IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                tuple(interaction_ids),
            ).fetchall()

        nodes_by_interaction: dict[str, list[CGIMemoryNode]] = {
            interaction_id: [] for interaction_id in interaction_ids
        }
        for row in node_rows:
            nodes_by_interaction[str(row["interaction_id"])].append(self._node_from_row(row))

        edges_by_interaction: dict[str, list[CGIMemoryEdge]] = {
            interaction_id: [] for interaction_id in interaction_ids
        }
        for row in edge_rows:
            edges_by_interaction[str(row["interaction_id"])].append(self._edge_from_row(row))

        return CGIMemoryTree(
            interactions=tuple(
                CGIMemoryInteraction(
                    id=str(row["id"]),
                    session_id=str(row["session_id"]) if row["session_id"] is not None else None,
                    user_message=str(row["user_message"]),
                    assistant_answer=str(row["assistant_answer"]),
                    parser_provider=str(row["parser_provider"]),
                    parser_model=str(row["parser_model"]),
                    created_at=str(row["created_at"]),
                    nodes=tuple(nodes_by_interaction[str(row["id"])]),
                    edges=tuple(edges_by_interaction[str(row["id"])]),
                )
                for row in interaction_rows
            )
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
            CREATE TABLE IF NOT EXISTS cgi_interactions (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                user_message TEXT NOT NULL,
                assistant_answer TEXT NOT NULL,
                parser_provider TEXT NOT NULL,
                parser_model TEXT NOT NULL,
                raw_document_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS cgi_nodes (
                id TEXT PRIMARY KEY,
                interaction_id TEXT NOT NULL REFERENCES cgi_interactions(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                weight REAL NOT NULL,
                tags_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cgi_nodes_interaction_id
            ON cgi_nodes(interaction_id);

            CREATE TABLE IF NOT EXISTS cgi_edges (
                id TEXT PRIMARY KEY,
                interaction_id TEXT NOT NULL REFERENCES cgi_interactions(id) ON DELETE CASCADE,
                source_label TEXT NOT NULL,
                target_label TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cgi_edges_interaction_id
            ON cgi_edges(interaction_id);
            """
        )

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> CGIMemoryNode:
        return CGIMemoryNode(
            id=str(row["id"]),
            interaction_id=str(row["interaction_id"]),
            label=str(row["label"]),
            kind=str(row["kind"]),
            summary=str(row["summary"]),
            weight=float(row["weight"]),
            tags=tuple(_decode_json_list(str(row["tags_json"]))),
            metadata=_decode_json_object(str(row["metadata_json"])),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> CGIMemoryEdge:
        return CGIMemoryEdge(
            id=str(row["id"]),
            interaction_id=str(row["interaction_id"]),
            source_label=str(row["source_label"]),
            target_label=str(row["target_label"]),
            relation=str(row["relation"]),
            weight=float(row["weight"]),
            metadata=_decode_json_object(str(row["metadata_json"])),
            created_at=str(row["created_at"]),
        )


def _decode_json_list(payload: str) -> list[str]:
    value = json.loads(payload)
    if not isinstance(value, list):
        raise ValueError("Stored CGI list payload is invalid")
    return [str(item) for item in value]


def _decode_json_object(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Stored CGI object payload is invalid")
    return dict(value)
