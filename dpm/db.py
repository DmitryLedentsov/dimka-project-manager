from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    repository_url TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT 'master',
    repo_path TEXT NOT NULL,
    auto_update INTEGER NOT NULL DEFAULT 1,
    poll_interval INTEGER NOT NULL DEFAULT 60,
    remote_commit TEXT,
    attempted_commit TEXT,
    deployed_commit TEXT,
    commit_message TEXT,
    commit_time TEXT,
    last_checked_at TEXT,
    last_deployed_at TEXT,
    deploy_status TEXT NOT NULL DEFAULT 'queued',
    deploy_stage TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    command_json TEXT NOT NULL,
    working_directory TEXT NOT NULL DEFAULT '.',
    environment_json TEXT NOT NULL DEFAULT '{}',
    environment_file TEXT,
    restart_policy TEXT NOT NULL DEFAULT 'always',
    healthcheck_json TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'stopped',
    pid INTEGER,
    started_at TEXT,
    stopped_at TEXT,
    exit_code INTEGER,
    restart_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    log_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    commit_sha TEXT,
    status TEXT NOT NULL,
    stage TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    log_path TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_services_project ON services(project_id);
CREATE INDEX IF NOT EXISTS idx_deployments_project ON deployments(project_id, id DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            connection = self.connect()
            try:
                cursor = connection.execute(sql, tuple(params))
                connection.commit()
                return int(cursor.lastrowid)
            finally:
                connection.close()

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock:
            connection = self.connect()
            try:
                connection.executemany(sql, rows)
                connection.commit()
            finally:
                connection.close()

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        self.execute(
            f"UPDATE {table} SET {columns} WHERE id = ?",
            [*values.values(), row_id],
        )

    @staticmethod
    def decode_json(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
