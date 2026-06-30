"""SQLite-backed CGI memory write boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.cgi_schema import CGIMemoryDocument

CGIParseJobStatus = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "QUOTA_LOCKED"]
_VALID_JOB_STATUSES: set[str] = {
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "QUOTA_LOCKED",
}


@dataclass(frozen=True, slots=True)
class CGIMemoryRecord:
    """Stored CGI parse result metadata."""

    interaction_id: str
    node_count: int
    edge_count: int


@dataclass(frozen=True, slots=True)
class CGIMemoryMaintenanceResult:
    """Summary from memory graph pruning/compaction."""

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


@dataclass(frozen=True, slots=True)
class CGIPruningEvent:
    """A persisted pruning event for dashboard proof and audits."""

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
class CGIParseJobWrite:
    """A completed visible answer queued for background CGI JSON parsing."""

    session_id: str | None
    user_message: str
    assistant_answer: str
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class CGIParseJobRecord:
    """A persisted CGI parse job for dashboard monitoring and retry controls."""

    id: str
    session_id: str | None
    user_message: str
    assistant_answer: str
    status: CGIParseJobStatus
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


@dataclass(frozen=True, slots=True)
class CGIStaleJobRecoveryResult:
    """Summary of stale PROCESSING jobs recovered by the zombie cleaner."""

    scanned: int
    requeued: int
    failed: int
    job_ids: tuple[str, ...]


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

    def enqueue_parse_job(self, write: CGIParseJobWrite) -> CGIParseJobRecord:
        """Persist a background CGI parse job before any LLM parse work begins."""

        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cgi_parse_jobs (
                    id,
                    session_id,
                    user_message,
                    assistant_answer,
                    status,
                    attempts,
                    max_attempts,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'PENDING', 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    job_id,
                    write.session_id,
                    write.user_message,
                    write.assistant_answer,
                    write.max_attempts,
                ),
            )
        record = self.get_parse_job(job_id)
        if record is None:
            raise RuntimeError("Saved CGI parse job could not be reloaded")
        return record

    def claim_next_parse_job(self) -> CGIParseJobRecord | None:
        """Atomically claim one due parse job for this worker process."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM cgi_parse_jobs
                    WHERE status = 'PENDING'
                      AND (next_run_at IS NULL OR next_run_at <= CURRENT_TIMESTAMP)
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                connection.execute(
                    """
                    UPDATE cgi_parse_jobs
                    SET
                        status = 'PROCESSING',
                        attempts = attempts + 1,
                        locked_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        claimed = self.get_parse_job(str(row["id"]))
        if claimed is None:
            raise RuntimeError("Claimed CGI parse job could not be reloaded")
        return claimed

    def complete_parse_job(self, job_id: str, *, interaction_id: str) -> CGIParseJobRecord:
        """Mark a parse job completed after memory graph persistence succeeds."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE cgi_parse_jobs
                SET
                    status = 'COMPLETED',
                    last_error_type = NULL,
                    last_error_message = NULL,
                    next_run_at = NULL,
                    locked_at = NULL,
                    completed_at = CURRENT_TIMESTAMP,
                    interaction_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (interaction_id, job_id),
            )
        record = self.get_parse_job(job_id)
        if record is None:
            raise RuntimeError("Completed CGI parse job could not be reloaded")
        return record

    def reschedule_parse_job(
        self,
        job_id: str,
        *,
        next_run_at: str,
        error_type: str,
        error_message: str,
    ) -> CGIParseJobRecord:
        """Move a failed transient job back to PENDING for a later retry."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE cgi_parse_jobs
                SET
                    status = 'PENDING',
                    last_error_type = ?,
                    last_error_message = ?,
                    next_run_at = ?,
                    locked_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_type, error_message, next_run_at, job_id),
            )
        record = self.get_parse_job(job_id)
        if record is None:
            raise RuntimeError("Rescheduled CGI parse job could not be reloaded")
        return record

    def mark_parse_job_failed(
        self,
        job_id: str,
        *,
        status: CGIParseJobStatus,
        error_type: str,
        error_message: str,
    ) -> CGIParseJobRecord:
        """Mark a job permanently stopped as FAILED or QUOTA_LOCKED."""

        if status not in {"FAILED", "QUOTA_LOCKED"}:
            raise ValueError("Only FAILED or QUOTA_LOCKED jobs can be marked as stopped")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE cgi_parse_jobs
                SET
                    status = ?,
                    last_error_type = ?,
                    last_error_message = ?,
                    next_run_at = NULL,
                    locked_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error_type, error_message, job_id),
            )
        record = self.get_parse_job(job_id)
        if record is None:
            raise RuntimeError("Failed CGI parse job could not be reloaded")
        return record

    def reset_parse_jobs_for_retry(
        self,
        *,
        statuses: tuple[CGIParseJobStatus, ...] = ("FAILED", "QUOTA_LOCKED"),
    ) -> int:
        """Move stopped jobs back to PENDING for user-triggered retry-all."""

        _validate_job_statuses(statuses)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE cgi_parse_jobs
                SET
                    status = 'PENDING',
                    attempts = 0,
                    last_error_type = NULL,
                    last_error_message = NULL,
                    next_run_at = NULL,
                    locked_at = NULL,
                    completed_at = NULL,
                    interaction_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ({placeholders})
                """,
                tuple(statuses),
            )
        return int(cursor.rowcount)

    def recover_stale_parse_jobs(
        self,
        *,
        stale_after_seconds: int,
        limit: int = 100,
    ) -> CGIStaleJobRecoveryResult:
        """Recover PROCESSING jobs whose worker lock outlived the configured TTL."""

        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")
        if limit < 1:
            raise ValueError("limit must be positive")

        cutoff_modifier = f"-{int(stale_after_seconds)} seconds"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT id, attempts, max_attempts
                    FROM cgi_parse_jobs
                    WHERE status = 'PROCESSING'
                      AND locked_at IS NOT NULL
                      AND locked_at <= DATETIME(CURRENT_TIMESTAMP, ?)
                    ORDER BY locked_at ASC, created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (cutoff_modifier, limit),
                ).fetchall()

                requeued = 0
                failed = 0
                job_ids: list[str] = []
                for row in rows:
                    job_id = str(row["id"])
                    job_ids.append(job_id)
                    attempts = int(row["attempts"])
                    max_attempts = int(row["max_attempts"])
                    if attempts < max_attempts:
                        connection.execute(
                            """
                            UPDATE cgi_parse_jobs
                            SET
                                status = 'PENDING',
                                last_error_type = 'stale_lock_recovered',
                                last_error_message = ?,
                                next_run_at = CURRENT_TIMESTAMP,
                                locked_at = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (
                                (
                                    "Recovered stale PROCESSING job after "
                                    f"{stale_after_seconds} seconds"
                                ),
                                job_id,
                            ),
                        )
                        requeued += 1
                    else:
                        connection.execute(
                            """
                            UPDATE cgi_parse_jobs
                            SET
                                status = 'FAILED',
                                last_error_type = 'stale_lock_exhausted',
                                last_error_message = ?,
                                next_run_at = NULL,
                                locked_at = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (
                                (
                                    "Stale PROCESSING job exhausted retry budget "
                                    f"after {attempts}/{max_attempts} attempts"
                                ),
                                job_id,
                            ),
                        )
                        failed += 1
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise

        return CGIStaleJobRecoveryResult(
            scanned=len(rows),
            requeued=requeued,
            failed=failed,
            job_ids=tuple(job_ids),
        )

    def get_parse_job(self, job_id: str) -> CGIParseJobRecord | None:
        """Return one parse job by id."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cgi_parse_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._parse_job_from_row(row)

    def list_parse_jobs(
        self,
        *,
        statuses: tuple[CGIParseJobStatus, ...] | None = None,
        limit: int = 100,
    ) -> list[CGIParseJobRecord]:
        """List parse jobs newest-first for dashboard monitoring."""

        if statuses:
            _validate_job_statuses(statuses)
            placeholders = ",".join("?" for _ in statuses)
            query = f"""
                SELECT *
                FROM cgi_parse_jobs
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (*statuses, limit)
        else:
            query = """
                SELECT *
                FROM cgi_parse_jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params = (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._parse_job_from_row(row) for row in rows]

    def count_nodes(self) -> int:
        """Return stored node count for tests and later dashboard wiring."""

        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cgi_nodes").fetchone()
        return int(row[0])

    def count_interactions(self) -> int:
        """Return stored interaction count for stress tests and dashboard metrics."""

        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cgi_interactions").fetchone()
        return int(row[0])

    def count_edges(self) -> int:
        """Return stored edge count for stress tests and dashboard metrics."""

        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cgi_edges").fetchone()
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

    def get_tree(
        self,
        *,
        limit_interactions: int = 50,
        session_id: str | None = None,
    ) -> CGIMemoryTree:
        """Return recent interactions grouped with nodes and edges as a JSON tree."""

        with self._connect() as connection:
            if session_id is None:
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
            else:
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
                    WHERE session_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (session_id, limit_interactions),
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

    def prune(
        self,
        *,
        max_interactions: int,
        min_node_weight: float,
        strategy: str = "escape_node_pruner",
    ) -> CGIMemoryMaintenanceResult:
        """Prune low-weight nodes and old interactions to stop CGI graph explosion."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                before_interactions = self._count_table(connection, "cgi_interactions")
                before_nodes = self._count_table(connection, "cgi_nodes")
                before_edges = self._count_table(connection, "cgi_edges")

                low_weight_rows = connection.execute(
                    """
                    SELECT interaction_id, label
                    FROM cgi_nodes
                    WHERE weight < ?
                    """,
                    (min_node_weight,),
                ).fetchall()
                for row in low_weight_rows:
                    connection.execute(
                        """
                        DELETE FROM cgi_edges
                        WHERE interaction_id = ?
                          AND (source_label = ? OR target_label = ?)
                        """,
                        (row["interaction_id"], row["label"], row["label"]),
                    )
                connection.execute(
                    "DELETE FROM cgi_nodes WHERE weight < ?",
                    (min_node_weight,),
                )

                old_interaction_rows = connection.execute(
                    """
                    SELECT id
                    FROM cgi_interactions
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (max_interactions,),
                ).fetchall()
                old_interaction_ids = [str(row["id"]) for row in old_interaction_rows]
                if old_interaction_ids:
                    placeholders = ",".join("?" for _ in old_interaction_ids)
                    connection.execute(
                        f"DELETE FROM cgi_interactions WHERE id IN ({placeholders})",
                        tuple(old_interaction_ids),
                    )

                after_interactions = self._count_table(connection, "cgi_interactions")
                after_nodes = self._count_table(connection, "cgi_nodes")
                after_edges = self._count_table(connection, "cgi_edges")

                result = CGIMemoryMaintenanceResult(
                    strategy=strategy,
                    before_interactions=before_interactions,
                    after_interactions=after_interactions,
                    before_nodes=before_nodes,
                    after_nodes=after_nodes,
                    before_edges=before_edges,
                    after_edges=after_edges,
                    pruned_interactions=before_interactions - after_interactions,
                    pruned_nodes=before_nodes - after_nodes,
                    pruned_edges=before_edges - after_edges,
                )
                connection.execute(
                    """
                    INSERT INTO cgi_pruning_events (
                        id,
                        strategy,
                        before_interactions,
                        after_interactions,
                        before_nodes,
                        after_nodes,
                        before_edges,
                        after_edges,
                        pruned_interactions,
                        pruned_nodes,
                        pruned_edges,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        uuid.uuid4().hex,
                        result.strategy,
                        result.before_interactions,
                        result.after_interactions,
                        result.before_nodes,
                        result.after_nodes,
                        result.before_edges,
                        result.after_edges,
                        result.pruned_interactions,
                        result.pruned_nodes,
                        result.pruned_edges,
                    ),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        return result

    def list_pruning_events(self, *, limit: int = 50) -> list[CGIPruningEvent]:
        """List recent pruning events newest-first."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    strategy,
                    before_interactions,
                    after_interactions,
                    before_nodes,
                    after_nodes,
                    before_edges,
                    after_edges,
                    pruned_interactions,
                    pruned_nodes,
                    pruned_edges,
                    created_at
                FROM cgi_pruning_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            CGIPruningEvent(
                id=str(row["id"]),
                strategy=str(row["strategy"]),
                before_interactions=int(row["before_interactions"]),
                after_interactions=int(row["after_interactions"]),
                before_nodes=int(row["before_nodes"]),
                after_nodes=int(row["after_nodes"]),
                before_edges=int(row["before_edges"]),
                after_edges=int(row["after_edges"]),
                pruned_interactions=int(row["pruned_interactions"]),
                pruned_nodes=int(row["pruned_nodes"]),
                pruned_edges=int(row["pruned_edges"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

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

            CREATE TABLE IF NOT EXISTS cgi_pruning_events (
                id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                before_interactions INTEGER NOT NULL,
                after_interactions INTEGER NOT NULL,
                before_nodes INTEGER NOT NULL,
                after_nodes INTEGER NOT NULL,
                before_edges INTEGER NOT NULL,
                after_edges INTEGER NOT NULL,
                pruned_interactions INTEGER NOT NULL,
                pruned_nodes INTEGER NOT NULL,
                pruned_edges INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cgi_pruning_events_created_at
            ON cgi_pruning_events(created_at);

            CREATE TABLE IF NOT EXISTS cgi_parse_jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                user_message TEXT NOT NULL,
                assistant_answer TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'QUOTA_LOCKED')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error_type TEXT,
                last_error_message TEXT,
                next_run_at TEXT,
                locked_at TEXT,
                completed_at TEXT,
                interaction_id TEXT REFERENCES cgi_interactions(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cgi_parse_jobs_status_next_run_at
            ON cgi_parse_jobs(status, next_run_at, created_at);

            CREATE INDEX IF NOT EXISTS idx_cgi_parse_jobs_interaction_id
            ON cgi_parse_jobs(interaction_id);
            """
        )

    @staticmethod
    def _count_table(connection: sqlite3.Connection, table: str) -> int:
        if table not in {"cgi_interactions", "cgi_nodes", "cgi_edges"}:
            raise ValueError("Unsupported count table")
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

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

    @staticmethod
    def _parse_job_from_row(row: sqlite3.Row) -> CGIParseJobRecord:
        status = str(row["status"])
        if status not in _VALID_JOB_STATUSES:
            raise ValueError(f"Stored CGI parse job has invalid status: {status}")
        return CGIParseJobRecord(
            id=str(row["id"]),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            user_message=str(row["user_message"]),
            assistant_answer=str(row["assistant_answer"]),
            status=status,  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            last_error_type=(
                str(row["last_error_type"]) if row["last_error_type"] is not None else None
            ),
            last_error_message=(
                str(row["last_error_message"]) if row["last_error_message"] is not None else None
            ),
            next_run_at=str(row["next_run_at"]) if row["next_run_at"] is not None else None,
            locked_at=str(row["locked_at"]) if row["locked_at"] is not None else None,
            completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
            interaction_id=(
                str(row["interaction_id"]) if row["interaction_id"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
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


def _validate_job_statuses(statuses: tuple[CGIParseJobStatus, ...]) -> None:
    for status in statuses:
        if status not in _VALID_JOB_STATUSES:
            raise ValueError(f"Unsupported CGI parse job status: {status}")
