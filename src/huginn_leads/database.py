from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, TypeVar

T = TypeVar("T")


class MigrationError(RuntimeError):
    """A migration failed without exposing SQL or business values."""


def _migration_sql(connection: sqlite3.Connection, script: str) -> None:
    # executescript() commits an existing transaction. Let SQLite identify complete
    # statements instead; transaction control belongs exclusively to the runner.
    def authorize(action, _arg1, _arg2, _database, _trigger):
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        statement = ""
        for char in script:
            statement += char
            if char == ";" and sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip():
            connection.execute(statement)
    finally:
        connection.set_authorizer(None)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, migration_dir: Path):
        self.path = path
        self.migration_dir = migration_dir
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def run() -> None:
            connection = self._connect()
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for path in sorted(self.migration_dir.glob("*.sql")):
                    version = int(path.stem.split("_", 1)[0])
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        if not connection.execute(
                            "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
                        ).fetchone():
                            _migration_sql(connection, path.read_text(encoding="utf-8"))
                            connection.execute(
                                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                                (version, utcnow()),
                            )
                        connection.commit()
                    except Exception as exc:
                        connection.rollback()
                        code = getattr(exc, "sqlite_errorname", type(exc).__name__)
                        raise MigrationError(
                            f"schema_migration_failed version={version} code={code}. "
                            "本次迁移已回滚；修复故障后可重试。若旧库结构与版本登记不一致，"
                            "请停止应用、先备份并核对结构与数据；不要跳过错误或直接补版本号。"
                            "恢复步骤：docs/数据库迁移恢复.md"
                        ) from None
            finally:
                connection.close()

        async with self._write_lock:
            await asyncio.to_thread(run)

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            connection = self._connect()
            try:
                return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        def run() -> int:
            connection = self._connect()
            try:
                cursor = connection.execute(sql, tuple(params))
                connection.commit()
                return cursor.rowcount
            finally:
                connection.close()

        async with self._write_lock:
            return await asyncio.to_thread(run)

    async def write(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = callback(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
            finally:
                connection.close()

        async with self._write_lock:
            return await asyncio.to_thread(run)

    async def get_setting(self, key: str) -> str | None:
        row = await self.fetch_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        now = utcnow()
        await self.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now),
        )

    async def integrity_check(self) -> bool:
        row = await self.fetch_one("PRAGMA integrity_check")
        return bool(row and next(iter(row.values())) == "ok")
