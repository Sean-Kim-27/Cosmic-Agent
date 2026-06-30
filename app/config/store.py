"""SQLite repository for runtime settings and model routing."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class StoredSetting:
    """A serialized runtime setting."""

    key: str
    value: str
    is_secret: bool


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Map a user-facing model alias to a provider-specific model id."""

    alias: str
    provider: str
    model: str


class SQLiteSettingsStore:
    """Small, connection-per-operation SQLite repository."""

    def __init__(self, path: Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = path.expanduser()
        self.timeout_seconds = timeout_seconds

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._enable_wal_if_available(connection)
            apply_migrations(connection)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Some platforms and mounted filesystems do not expose POSIX permissions.
            pass

    def set_setting(self, setting: StoredSetting) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_settings (key, value, is_secret, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (setting.key, setting.value, int(setting.is_secret)),
            )

    def delete_setting(self, key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_settings WHERE key = ?",
                (key,),
            )
        return cursor.rowcount > 0

    def list_settings(self) -> list[StoredSetting]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT key, value, is_secret
                FROM runtime_settings
                ORDER BY key
                """
            ).fetchall()
        return [
            StoredSetting(
                key=str(row["key"]),
                value=str(row["value"]),
                is_secret=bool(row["is_secret"]),
            )
            for row in rows
        ]

    def upsert_model_route(self, route: ModelRoute) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_routes (alias, provider, model, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(alias) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (route.alias, route.provider, route.model),
            )

    def get_model_route(self, alias: str) -> ModelRoute | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT alias, provider, model
                FROM model_routes
                WHERE alias = ?
                """,
                (alias,),
            ).fetchone()
        if row is None:
            return None
        return ModelRoute(
            alias=str(row["alias"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
        )

    def list_model_routes(self) -> list[ModelRoute]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT alias, provider, model
                FROM model_routes
                ORDER BY alias
                """
            ).fetchall()
        return [
            ModelRoute(
                alias=str(row["alias"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
            )
            for row in rows
        ]

    def delete_model_route(self, alias: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM model_routes WHERE alias = ?",
                (alias,),
            )
        return cursor.rowcount > 0

    def applied_migration_versions(self) -> tuple[int, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(int(row["version"]) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
        )
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
            # Another worker is initializing the same file. Its WAL change or
            # SQLite's default journal mode are both safe for this short setup.
