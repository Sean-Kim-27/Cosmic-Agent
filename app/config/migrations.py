"""Versioned SQLite schema migrations for runtime configuration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    """An immutable, forward-only schema migration."""

    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        description="create runtime settings and model routes",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                is_secret INTEGER NOT NULL CHECK (is_secret IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_routes (
                alias TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    ),
)


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply all pending migrations and record each version atomically."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    connection.commit()

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES (?, ?)
                """,
                (migration.version, migration.description),
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
