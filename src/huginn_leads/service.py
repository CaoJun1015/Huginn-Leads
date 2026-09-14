from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import uuid
from typing import Any

from .config import Settings
from .database import Database, utcnow
from .rules import PROCESSOR_VERSION, evaluate


def _uuid() -> str:
    return str(uuid.uuid4())


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


class Workbench:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.worker_task: asyncio.Task[None] | None = None
        self.maintenance_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        defaults = {
            "raw_days": str(self.settings.raw_days),
            "lead_days": str(self.settings.lead_days),
            "diagnostic_days": str(self.settings.diagnostic_days),
            "backup_keep": str(self.settings.backup_keep),
        }
        for key, value in defaults.items():
            if await self.database.get_setting(key) is None:
                await self.database.set_setting(key, value)
        self.worker_task = asyncio.create_task(self._worker_loop(), name="huginn-leads-worker")
        self.maintenance_task = asyncio.create_task(self._maintenance_loop(), name="huginn-leads-maintenance")

    async def stop(self) -> None:
        self.stop_event.set()
        for task in (self.worker_task, self.maintenance_task):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.worker_task, self.maintenance_task) if task), return_exceptions=True)

    async def record_diagnostic(
        self,
        code: str,
        account_id: str | None = None,
        source_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.database.execute(
            "INSERT INTO diagnostics(occurred_at, code, account_id, source_id, details_json) VALUES (?, ?, ?, ?, ?)",
            (utcnow(), code, account_id, source_id, json.dumps(details or {}, ensure_ascii=False)),
        )

    async def connect_account(self, account_id: str, platform_name: str | None) -> None:
        now = utcnow()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO accounts(account_id, platform_name, connected, last_seen_at, updated_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                     platform_name=COALESCE(excluded.platform_name, accounts.platform_name),
                     connected=1, last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at,
                     version=accounts.version+1""",
                (account_id, platform_name, now, now),
            )
            connection.execute(
                "UPDATE connection_gaps SET ended_at=? WHERE account_id=? AND ended_at IS NULL",
                (now, account_id),
            )

        await self.database.write(write)

    async def disconnect_account(self, account_id: str) -> None:
        now = utcnow()

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE accounts SET connected=0, updated_at=?, version=version+1 WHERE account_id=?",
                (now, account_id),
            )
            exists = connection.execute(
                "SELECT 1 FROM connection_gaps WHERE account_id=? AND ended_at IS NULL", (account_id,)
            ).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO connection_gaps(id, account_id, started_at, reason) VALUES (?, ?, ?, 'websocket_disconnected')",
                    (_uuid(), account_id, now),
                )

        await self.database.write(write)

    async def touch_account(self, account_id: str) -> None:
        now = utcnow()
        await self.database.execute(
            "UPDATE accounts SET last_seen_at=?, updated_at=?, version=version+1 WHERE account_id=?",
            (now, now, account_id),
        )

    async def refresh_sources(
        self, account_id: str, items: list[dict[str, Any]], source_type: str = "group"
    ) -> int:
        if source_type not in {"group", "contact"}:
            raise ValueError("来源类型无效")
        now = utcnow()

        def write(connection: sqlite3.Connection) -> int:
            current_ids: set[str] = set()
            for item in items:
                peer_id = str(item.get("group_id" if source_type == "group" else "user_id", ""))
                if not peer_id:
                    continue
                current_ids.add(peer_id)
                raw_name = item.get("group_name") if source_type == "group" else item.get("remark") or item.get("nickname")
                name = str(raw_name or "").strip() or None
                storage_key = peer_id if source_type == "group" else f"contact:{peer_id}"
                connection.execute(
                    """INSERT INTO sources(account_id, group_id, platform_name, enabled, updated_at, source_type, peer_id)
                       VALUES (?, ?, ?, 0, ?, ?, ?)
                       ON CONFLICT(account_id, source_type, peer_id) DO UPDATE SET
                         platform_name=excluded.platform_name, error_code=NULL,
                         updated_at=excluded.updated_at, version=sources.version+1""",
                    (account_id, storage_key, name, now, source_type, peer_id),
                )
            rows = connection.execute(
                "SELECT id, peer_id FROM sources WHERE account_id=? AND source_type=?", (account_id, source_type)
            ).fetchall()
            for row in rows:
                if row["peer_id"] not in current_ids:
                    connection.execute(
                        "UPDATE sources SET enabled=0, error_code=?, updated_at=?, version=version+1 WHERE id=?",
                        ("not_in_group" if source_type == "group" else "not_in_contacts", now, row["id"]),
                    )
            connection.execute(
                "INSERT INTO audit(occurred_at, object_type, object_id, action, count) VALUES (?, 'account', ?, 'refresh_sources', ?)",
                (now, account_id, len(current_ids)),
            )
            return len(current_ids)

        return await self.database.write(write)

    async def upsert_source(self, account_id: str, group: dict[str, Any]) -> dict[str, Any]:
        group_id = str(group.get("group_id", ""))
        if not group_id:
            raise ValueError("群号无效")
        name = str(group.get("group_name") or "").strip() or None
        now = utcnow()
        await self.database.execute(
            """INSERT INTO sources(account_id, group_id, platform_name, enabled, updated_at, source_type, peer_id)
               VALUES (?, ?, ?, 0, ?, 'group', ?)
               ON CONFLICT(account_id, source_type, peer_id) DO UPDATE SET
                 platform_name=excluded.platform_name, error_code=NULL,
                 updated_at=excluded.updated_at, version=sources.version+1""",
            (account_id, group_id, name, now, group_id),
        )
        row = await self.database.fetch_one(
            "SELECT * FROM sources WHERE account_id=? AND source_type='group' AND peer_id=?", (account_id, group_id)
        )
        return row or {}

    async def ingest(self, message: dict[str, Any]) -> dict[str, Any]:
        account_id = str(message.get("account_id", ""))
        source_type = str(message.get("source_type", "group"))
        peer_id = str(message.get("peer_id") or message.get("group_id", ""))
        native_id = str(message.get("native_message_id", ""))
        if not native_id:
            await self.record_diagnostic("missing_message_id", account_id)
            return {"saved": False, "reason": "missing_message_id"}
        source = await self.database.fetch_one(
            """SELECT s.id FROM sources s JOIN accounts a ON a.account_id=s.account_id
               WHERE s.account_id=? AND s.source_type=? AND s.peer_id=? AND s.enabled=1 AND a.connected=1""",
            (account_id, source_type, peer_id),
        )
        if not source:
            await self.record_diagnostic("source_not_enabled", account_id)
            return {"saved": False, "reason": "source_not_enabled"}

        raw_days = int(await self.database.get_setting("raw_days") or self.settings.raw_days)
        now = utcnow()
        message_id = _uuid()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            storage_key = peer_id if source_type == "group" else f"contact:{peer_id}"
            existing = connection.execute(
                """SELECT id FROM raw_messages
                   WHERE platform=? AND source_id=? AND native_message_id=?""",
                (message["platform"], source["id"], native_id),
            ).fetchone()
            if existing:
                return {"saved": True, "duplicate": True, "message_id": existing["id"], "jobs": 0}
            connection.execute(
                """INSERT INTO raw_messages(
                     id, platform, account_id, group_id, native_message_id, sender_id, sender_name,
                     message_time, received_at, text, segments_json, expires_at, created_at,
                     source_id, source_type, peer_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    message["platform"],
                    account_id,
                    storage_key,
                    native_id,
                    str(message["sender_id"]),
                    message.get("sender_name"),
                    message["message_time"],
                    now,
                    message.get("text", ""),
                    json.dumps(message.get("segments", []), ensure_ascii=False),
                    _days(raw_days),
                    now,
                    source["id"],
                    source_type,
                    peer_id,
                ),
            )
            active_count = connection.execute(
                "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            job_status = "pending" if active_count < self.settings.max_active_jobs else "deferred"
            tasks = connection.execute(
                """SELECT t.id, t.current_revision FROM tasks t
                   JOIN task_revision_sources trs ON trs.task_id=t.id AND trs.revision=t.current_revision
                   WHERE trs.source_id=? AND t.status='active' AND (t.expires_at IS NULL OR t.expires_at>?)""",
                (source["id"], now),
            ).fetchall()
            for task in tasks:
                connection.execute(
                    """INSERT OR IGNORE INTO processing_jobs(
                         id, message_id, task_id, revision, processor_version, status,
                         next_attempt_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_uuid(), message_id, task["id"], task["current_revision"], PROCESSOR_VERSION, job_status, now, now, now),
                )
            connection.execute(
                "UPDATE sources SET last_saved_at=?, error_code=NULL, updated_at=?, version=version+1 WHERE id=?",
                (now, now, source["id"]),
            )
            connection.execute(
                "UPDATE accounts SET last_seen_at=?, updated_at=?, version=version+1 WHERE account_id=?",
                (now, now, account_id),
            )
            return {"saved": True, "duplicate": False, "message_id": message_id, "jobs": len(tasks)}

        try:
            return await self.database.write(write)
        except sqlite3.Error as exc:
            await self.record_diagnostic("database_write_failed", account_id, source["id"], {"kind": type(exc).__name__})
            return {"saved": False, "reason": "database_write_failed"}

    async def _promote_deferred(self) -> None:
        active = await self.database.fetch_one(
            "SELECT COUNT(*) AS total FROM processing_jobs WHERE status IN ('pending','processing')"
        )
        room = max(0, self.settings.max_active_jobs - int(active["total"] if active else 0))
        if room:
            await self.database.execute(
                """UPDATE processing_jobs SET status='pending', updated_at=? WHERE id IN
                   (SELECT id FROM processing_jobs WHERE status='deferred' ORDER BY created_at LIMIT ?)""",
                (utcnow(), room),
            )

    async def _claim_job(self) -> dict[str, Any] | None:
        now = utcnow()
        lease = _future(60)

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            connection.execute(
                """UPDATE processing_jobs SET status=CASE WHEN attempts>=4 THEN 'failed' ELSE 'pending' END,
                   error_code=CASE WHEN attempts>=4 THEN 'lease_expired' ELSE error_code END,
                   next_attempt_at=?, lease_until=NULL, updated_at=?
                   WHERE status='processing' AND lease_until<?""",
                (now, now, now),
            )
            row = connection.execute(
                """SELECT * FROM processing_jobs
                   WHERE status='pending' AND next_attempt_at<=?
                   ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE processing_jobs SET status='processing', attempts=attempts+1, lease_until=?, updated_at=? WHERE id=?",
                (lease, now, row["id"]),
            )
            return dict(row) | {"attempts": row["attempts"] + 1, "status": "processing"}

        return await self.database.write(write)

    async def _process_job(self, job: dict[str, Any]) -> None:
        row = await self.database.fetch_one(
            """SELECT j.*, r.text, r.received_at, tr.conditions_json, tpl.fields_json,
               t.status AS task_status, t.expires_at
               FROM processing_jobs j
               LEFT JOIN raw_messages r ON r.id=j.message_id
               JOIN task_revisions tr ON tr.task_id=j.task_id AND tr.revision=j.revision
               LEFT JOIN template_revisions tpl ON tpl.template_id=(SELECT template_id FROM tasks WHERE id=j.task_id)
                    AND tpl.version=CAST(tr.template_version AS INTEGER)
               JOIN tasks t ON t.id=j.task_id WHERE j.id=?""",
            (job["id"],),
        )
        if not row or row["text"] is None:
            await self.database.execute(
                "UPDATE processing_jobs SET status='source_expired', lease_until=NULL, updated_at=? WHERE id=?",
                (utcnow(), job["id"]),
            )
            return
        if row["task_status"] != "active" or (row["expires_at"] and row["expires_at"] <= utcnow()):
            await self.database.execute(
                "UPDATE processing_jobs SET status='cancelled', lease_until=NULL, updated_at=? WHERE id=?",
                (utcnow(), job["id"]),
            )
            return
        conditions = json.loads(row["conditions_json"])
        fields = json.loads(row["fields_json"]) if row.get("fields_json") else None
        results = evaluate(row["text"], conditions, fields)
        now = utcnow()
        lead_days = int(await self.database.get_setting("lead_days") or self.settings.lead_days)

        def write(connection: sqlite3.Connection) -> None:
            task_state = connection.execute(
                "SELECT status, expires_at FROM tasks WHERE id=?", (row["task_id"],)
            ).fetchone()
            if not task_state or task_state["status"] != "active" or (
                task_state["expires_at"] and task_state["expires_at"] <= now
            ):
                connection.execute(
                    "UPDATE processing_jobs SET status='cancelled', lease_until=NULL, updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                return
            outcome = "not_match" if not results else (
                "conflict" if any(item["outcome"] == "conflict" for item in results) else
                "review" if any(item["outcome"] == "review" for item in results) else "match"
            )
            reason = "关键词未命中" if not results else "；".join(dict.fromkeys(item["reason"] for item in results))
            payload = json.dumps(results, ensure_ascii=False)
            connection.execute(
                """INSERT INTO evaluations(id, message_id, task_id, revision, processor_version, outcome, reason, result_json, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id, task_id, revision, processor_version) DO UPDATE SET
                     outcome=excluded.outcome, reason=excluded.reason, result_json=excluded.result_json,
                     processed_at=excluded.processed_at""",
                (_uuid(), row["message_id"], row["task_id"], row["revision"], PROCESSOR_VERSION, outcome, reason, payload, now),
            )
            keys = [item["item_key"] for item in results]
            for item in results:
                connection.execute(
                    """INSERT INTO leads(
                         id, task_id, message_id, item_key, revision, processor_version, direction,
                         extracted_json, evidence_json, comparison_json, outcome, reason,
                         evidence_expires_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(task_id, message_id, item_key) DO UPDATE SET
                         revision=excluded.revision, processor_version=excluded.processor_version,
                         direction=excluded.direction, extracted_json=excluded.extracted_json,
                         evidence_json=excluded.evidence_json, comparison_json=excluded.comparison_json,
                         outcome=excluded.outcome, reason=excluded.reason, superseded_at=NULL,
                         evidence_expires_at=excluded.evidence_expires_at, updated_at=excluded.updated_at,
                         version=leads.version+1""",
                    (
                        _uuid(), row["task_id"], row["message_id"], item["item_key"], row["revision"], PROCESSOR_VERSION,
                        item["direction"], json.dumps(item["extracted"], ensure_ascii=False),
                        json.dumps({"text": item["text"]}, ensure_ascii=False),
                        json.dumps(item["comparisons"], ensure_ascii=False), item["outcome"], item["reason"],
                        _days(lead_days), now, now,
                    ),
                )
            if keys:
                placeholders = ",".join("?" for _ in keys)
                connection.execute(
                    f"UPDATE leads SET superseded_at=?, updated_at=?, version=version+1 WHERE task_id=? AND message_id=? AND item_key NOT IN ({placeholders}) AND superseded_at IS NULL",
                    (now, now, row["task_id"], row["message_id"], *keys),
                )
            else:
                connection.execute(
                    "UPDATE leads SET superseded_at=?, updated_at=?, version=version+1 WHERE task_id=? AND message_id=? AND superseded_at IS NULL",
                    (now, now, row["task_id"], row["message_id"]),
                )
            connection.execute(
                "UPDATE processing_jobs SET status='done', lease_until=NULL, error_code=NULL, updated_at=? WHERE id=?",
                (now, job["id"]),
            )

        await self.database.write(write)

    async def _fail_job(self, job: dict[str, Any], exc: Exception) -> None:
        attempts = int(job["attempts"])
        retry_delays = [5, 30, 300]
        status = "failed" if attempts >= 4 else "pending"
        delay = retry_delays[min(attempts - 1, len(retry_delays) - 1)]
        await self.database.execute(
            """UPDATE processing_jobs SET status=?, next_attempt_at=?, lease_until=NULL,
               error_code=?, updated_at=? WHERE id=?""",
            (status, _future(delay), type(exc).__name__, utcnow(), job["id"]),
        )

    async def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._promote_deferred()
                job = await self._claim_job()
                if job:
                    try:
                        await self._process_job(job)
                    except Exception as exc:
                        await self._fail_job(job, exc)
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    async def cleanup(self) -> dict[str, int]:
        now = utcnow()
        diagnostic_days = int(await self.database.get_setting("diagnostic_days") or self.settings.diagnostic_days)
        diagnostic_before = (datetime.now(timezone.utc) - timedelta(days=diagnostic_days)).isoformat(timespec="seconds")

        def write(connection: sqlite3.Connection) -> dict[str, int]:
            jobs = connection.execute(
                "UPDATE processing_jobs SET status='source_expired', updated_at=? WHERE message_id IN (SELECT id FROM raw_messages WHERE expires_at<=?) AND status NOT IN ('done','failed','cancelled','source_expired')",
                (now, now),
            ).rowcount
            raw = connection.execute("DELETE FROM raw_messages WHERE expires_at<=?", (now,)).rowcount
            leads = connection.execute("DELETE FROM leads WHERE evidence_expires_at<=?", (now,)).rowcount
            diagnostics = connection.execute("DELETE FROM diagnostics WHERE occurred_at<?", (diagnostic_before,)).rowcount
            connection.execute(
                "INSERT INTO audit(occurred_at, object_type, action, count) VALUES (?, 'data', 'cleanup', ?)",
                (now, raw + leads + diagnostics),
            )
            return {"messages": raw, "leads": leads, "diagnostics": diagnostics, "expired_jobs": jobs}

        return await self.database.write(write)

    async def update_retention(self, values: dict[str, int]) -> None:
        def write(connection: sqlite3.Connection) -> None:
            for row in connection.execute("SELECT id, received_at FROM raw_messages").fetchall():
                received = datetime.fromisoformat(row["received_at"])
                connection.execute(
                    "UPDATE raw_messages SET expires_at=? WHERE id=?",
                    ((received + timedelta(days=values["raw_days"])).isoformat(timespec="seconds"), row["id"]),
                )
            for row in connection.execute("SELECT id, created_at FROM leads").fetchall():
                created = datetime.fromisoformat(row["created_at"])
                connection.execute(
                    "UPDATE leads SET evidence_expires_at=?, version=version+1 WHERE id=?",
                    ((created + timedelta(days=values["lead_days"])).isoformat(timespec="seconds"), row["id"]),
                )
            now = utcnow()
            for key, value in values.items():
                connection.execute(
                    """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    (key, str(value), now),
                )

        await self.database.write(write)

    async def backup(self) -> Path:
        target = self.settings.backup_dir / f"huginn-leads-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"

        def run() -> None:
            source = sqlite3.connect(self.settings.database_path)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

        await asyncio.to_thread(run)
        await self.database.execute(
            "INSERT INTO audit(occurred_at, object_type, object_id, action) VALUES (?, 'backup', ?, 'create')",
            (utcnow(), target.name),
        )
        await self._trim_backups()
        return target

    async def _trim_backups(self) -> None:
        keep = int(await self.database.get_setting("backup_keep") or self.settings.backup_keep)
        files = sorted(self.settings.backup_dir.glob("huginn-leads-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[keep:]:
            path.unlink(missing_ok=True)

    async def _maintenance_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.cleanup()
                last_backup = await self.database.get_setting("last_daily_backup")
                today = datetime.now().date().isoformat()
                if last_backup != today:
                    await self.backup()
                    await self.database.set_setting("last_daily_backup", today)
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    async def status(self) -> dict[str, Any]:
        counts = await self.database.fetch_one(
            """SELECT
               SUM(status IN ('pending','deferred','processing')) AS backlog,
               SUM(status='failed') AS failed FROM processing_jobs"""
        ) or {"backlog": 0, "failed": 0}
        account = await self.database.fetch_one(
            "SELECT SUM(connected) AS connected, MAX(last_seen_at) AS last_seen_at FROM accounts"
        ) or {"connected": 0, "last_seen_at": None}
        last_saved = await self.database.fetch_one("SELECT MAX(received_at) AS value FROM raw_messages")
        raw_count = await self.database.fetch_one("SELECT COUNT(*) AS value FROM raw_messages")
        lead_count = await self.database.fetch_one("SELECT COUNT(*) AS value FROM leads WHERE superseded_at IS NULL")
        usage = shutil.disk_usage(self.settings.data_dir)
        return {
            "accounts_connected": account["connected"] or 0,
            "last_received_at": account["last_seen_at"],
            "last_saved_at": last_saved["value"] if last_saved else None,
            "backlog": counts["backlog"] or 0,
            "failed": counts["failed"] or 0,
            "raw_messages": raw_count["value"] if raw_count else 0,
            "leads": lead_count["value"] if lead_count else 0,
            "disk_free_bytes": usage.free,
            "data_path": str(self.settings.data_dir),
            "backup_path": str(self.settings.backup_dir),
            "worker_running": bool(self.worker_task and not self.worker_task.done()),
            "maintenance_running": bool(self.maintenance_task and not self.maintenance_task.done()),
            "retention": {
                "raw_days": int(await self.database.get_setting("raw_days") or self.settings.raw_days),
                "lead_days": int(await self.database.get_setting("lead_days") or self.settings.lead_days),
                "diagnostic_days": int(await self.database.get_setting("diagnostic_days") or self.settings.diagnostic_days),
                "backup_keep": int(await self.database.get_setting("backup_keep") or self.settings.backup_keep),
            },
        }
