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
from .rules import PROCESSOR_VERSION, evaluate, is_candidate


def _uuid() -> str:
    return str(uuid.uuid4())


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def _task_expired(value: str | None, now: str) -> bool:
    if not value:
        return False
    if len(value) == 10:
        try:
            return datetime.fromisoformat(value).date() < datetime.now().astimezone().date()
        except ValueError:
            return True
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.fromisoformat(now)
    except ValueError:
        return True


def snapshot_lead_source(connection: sqlite3.Connection, task_id: str, message_id: str) -> None:
    """Capture provenance for manual and machine leads in their write transaction."""
    connection.execute(
        """UPDATE leads SET (platform, account_id, source_id, source_type, peer_id,
             source_name, native_message_id, message_time, received_at) = (
             SELECT r.platform, r.account_id, r.source_id, r.source_type, r.peer_id,
                    COALESCE(s.user_alias, s.platform_name, r.peer_id), r.native_message_id,
                    r.message_time, r.received_at
             FROM raw_messages r LEFT JOIN sources s ON s.id=r.source_id WHERE r.id=?
           ) WHERE task_id=? AND message_id=? AND received_at IS NULL""",
        (message_id, task_id, message_id),
    )


class Workbench:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.maintenance: dict[str, dict[str, Any]] = {}
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
        await self._mark_offline("restart_time_unknown")
        self.stop_event.clear()
        for operation in ("cleanup", "backup"):
            saved = await self.database.get_setting("maintenance_" + operation)
            self.maintenance[operation] = json.loads(saved) if saved else {"state": "unknown"}
        self.worker_task = asyncio.create_task(self._worker_loop(), name="huginn-leads-worker")
        self.maintenance_task = asyncio.create_task(self._maintenance_loop(), name="huginn-leads-maintenance")

    async def stop(self) -> None:
        self.stop_event.set()
        # Let in-flight SQLite/thread operations finish before releasing the process lock.
        await asyncio.gather(*(task for task in (self.worker_task, self.maintenance_task) if task), return_exceptions=True)
        await self._mark_offline("application_stopped")

    async def _mark_offline(self, reason: str) -> None:
        now = utcnow()
        def write(connection: sqlite3.Connection) -> None:
            for row in connection.execute("SELECT account_id FROM accounts WHERE connected=1").fetchall():
                if not connection.execute("SELECT 1 FROM connection_gaps WHERE account_id=? AND ended_at IS NULL", (row["account_id"],)).fetchone():
                    connection.execute(
                        "INSERT INTO connection_gaps(id,account_id,started_at,reason) VALUES (?,?,?,?)",
                        (_uuid(), row["account_id"], now, reason),
                    )
            connection.execute("UPDATE accounts SET connected=0, updated_at=?, version=version+1 WHERE connected=1", (now,))
        await self.database.write(write)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _record_maintenance(self, operation: str, error: Exception | None = None) -> None:
        now = utcnow()
        result = self.maintenance.setdefault(operation, {})
        result.update(state="error" if error else "ok", last_attempt_at=now)
        if error:
            result.update(last_failure_at=now, last_failure_code=getattr(error, "sqlite_errorname", type(error).__name__))
        else:
            result["last_success_at"] = now
        try:
            result.pop("status_storage_error", None)
            await self.database.set_setting("maintenance_" + operation, json.dumps(result))
        except Exception:
            # Still observable during a DB write outage, without unbounded diagnostic rows.
            result["status_storage_error"] = True

    async def _maintain(self, operation: str, action):
        try:
            result = await action()
        except Exception as exc:
            await self._record_maintenance(operation, exc)
            raise
        await self._record_maintenance(operation)
        return result

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
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE accounts SET last_seen_at=?, updated_at=?, version=version+1 WHERE account_id=?",
                (now, now, account_id),
            )
            connection.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES ('last_received_at',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (now, now),
            )
        await self.database.write(write)

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
        source_query = """SELECT s.id FROM sources s JOIN accounts a ON a.account_id=s.account_id
            WHERE s.account_id=? AND s.source_type=? AND s.peer_id=? AND s.enabled=1 AND a.connected=1"""
        source_params = (account_id, source_type, peer_id)
        source = await self.database.fetch_one(source_query, source_params)
        if not source:
            return {"saved": False, "reason": "source_not_enabled"}

        raw_days = int(await self.database.get_setting("raw_days") or self.settings.raw_days)
        now = utcnow()
        message_id = _uuid()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            # BEGIN IMMEDIATE serializes this final check and all inserts with
            # source disable/account disconnect, including other connections.
            source = connection.execute(source_query, source_params).fetchone()
            if not source:
                return {"saved": False, "reason": "source_not_enabled"}
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
                """SELECT t.id, t.current_revision, t.expires_at FROM tasks t
                   JOIN task_revision_sources trs ON trs.task_id=t.id AND trs.revision=t.current_revision
                   WHERE trs.source_id=? AND t.status='active'""",
                (source["id"],),
            ).fetchall()
            tasks = [task for task in tasks if not _task_expired(task["expires_at"], now)]
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
            connection.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES ('last_saved_at',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (now, now),
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
            connection.execute(
                """UPDATE replays SET
                     status=CASE WHEN EXISTS(
                       SELECT 1 FROM processing_jobs j WHERE j.replay_id=replays.id AND j.status='failed'
                     ) THEN 'failed' ELSE 'completed' END,
                     completed_at=?
                   WHERE status='running' AND NOT EXISTS(
                     SELECT 1 FROM processing_jobs j
                     WHERE j.replay_id=replays.id AND j.status IN ('pending','deferred','processing')
                   )""",
                (now,),
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
            """SELECT j.*, r.text, r.received_at, r.sender_id, r.sender_name,
               tr.conditions_json, tpl.fields_json,
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
        if row["task_status"] != "active" or _task_expired(row["expires_at"], utcnow()):
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
            current_job = connection.execute(
                "SELECT message_id, status FROM processing_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            # Cleanup/deletion may commit while evaluation runs outside the lock.
            if not current_job or current_job["status"] != "processing":
                return
            if current_job["message_id"] is None:
                connection.execute(
                    "UPDATE processing_jobs SET status='source_expired', lease_until=NULL, updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                return
            task_state = connection.execute(
                "SELECT status, expires_at FROM tasks WHERE id=?", (row["task_id"],)
            ).fetchone()
            if not task_state or task_state["status"] != "active" or _task_expired(task_state["expires_at"], now):
                connection.execute(
                    "UPDATE processing_jobs SET status='cancelled', lease_until=NULL, updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                return
            outcome = "not_match" if not results else (
                "conflict" if any(item["outcome"] == "conflict" for item in results) else
                "review" if any(item["outcome"] == "review" for item in results) else "match"
            )
            reason = (
                "未发现可执行交易意图" if not results and is_candidate(row["text"], conditions)
                else "关键词未命中" if not results
                else "；".join(dict.fromkeys(item["reason"] for item in results))
            )
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
                         extracted_json, evidence_json, comparison_json, outcome, reason, sender_id, sender_name,
                         evidence_expires_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(task_id, message_id, item_key) DO UPDATE SET
                         revision=excluded.revision, processor_version=excluded.processor_version,
                         direction=excluded.direction, extracted_json=excluded.extracted_json,
                         evidence_json=excluded.evidence_json, comparison_json=excluded.comparison_json,
                         outcome=excluded.outcome, reason=excluded.reason,
                         sender_id=excluded.sender_id, sender_name=excluded.sender_name, superseded_at=NULL,
                         evidence_expires_at=excluded.evidence_expires_at, updated_at=excluded.updated_at,
                         version=leads.version+1
                       WHERE leads.origin='machine'""",
                    (
                        _uuid(), row["task_id"], row["message_id"], item["item_key"], row["revision"], PROCESSOR_VERSION,
                        item["direction"], json.dumps(item["extracted"], ensure_ascii=False),
                        json.dumps({"text": item["text"]}, ensure_ascii=False),
                        json.dumps(item["comparisons"], ensure_ascii=False), item["outcome"], item["reason"],
                        row["sender_id"], row["sender_name"],
                        _days(lead_days), now, now,
                    ),
                )
            if keys:
                placeholders = ",".join("?" for _ in keys)
                connection.execute(
                    f"UPDATE leads SET superseded_at=?, updated_at=?, version=version+1 WHERE task_id=? AND message_id=? AND origin='machine' AND item_key NOT IN ({placeholders}) AND superseded_at IS NULL",
                    (now, now, row["task_id"], row["message_id"], *keys),
                )
            else:
                connection.execute(
                    "UPDATE leads SET superseded_at=?, updated_at=?, version=version+1 WHERE task_id=? AND message_id=? AND origin='machine' AND superseded_at IS NULL",
                    (now, now, row["task_id"], row["message_id"]),
                )
            snapshot_lead_source(connection, row["task_id"], row["message_id"])
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
                    await self._wait(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                await self._wait(1)

    async def cleanup(self) -> dict[str, int]:
        return await self._maintain("cleanup", self._cleanup)

    async def _cleanup(self) -> dict[str, int]:
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
        return await self._maintain("backup", self._backup)

    async def _backup(self) -> Path:
        target = self.settings.backup_dir / f"huginn-leads-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"

        def run() -> None:
            from contextlib import closing
            try:
                with closing(sqlite3.connect(self.settings.database_path)) as source, closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
                    destination.execute("PRAGMA journal_mode=DELETE")
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("backup_integrity_failed")
            except Exception:
                target.unlink(missing_ok=True)
                raise

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
            failed = False
            for operation in ("cleanup", "backup"):
                if self.stop_event.is_set():
                    break
                try:
                    if operation == "cleanup":
                        await self.cleanup()
                    else:
                        today = datetime.now().date().isoformat()
                        if await self.database.get_setting("last_daily_backup") != today or self.maintenance.get("backup", {}).get("state") == "error":
                            await self.backup()
                            await self.database.set_setting("last_daily_backup", today)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    # Also report scheduling/setting failures outside the operation itself.
                    if self.maintenance.get(operation, {}).get("state") != "error":
                        await self._record_maintenance(operation, exc)
                    failed = True
            await self._wait(60 if failed else 3600)

    async def status(self) -> dict[str, Any]:
        snapshot = await self.database.fetch_one(
            """SELECT
              COALESCE((SELECT SUM(status IN ('pending','deferred','processing')) FROM processing_jobs),0) AS backlog,
              COALESCE((SELECT SUM(status='failed') FROM processing_jobs),0) AS failed,
              COALESCE((SELECT SUM(connected) FROM accounts),0) AS connected,
              (SELECT value FROM settings WHERE key='last_received_at') AS last_received_at,
              COALESCE((SELECT value FROM settings WHERE key='last_saved_at'),
                       (SELECT MAX(last_saved_at) FROM sources)) AS last_saved_at,
              (SELECT value FROM settings WHERE key='maintenance_cleanup') AS maintenance_cleanup,
              (SELECT value FROM settings WHERE key='maintenance_backup') AS maintenance_backup,
              (SELECT COUNT(*) FROM raw_messages) AS raw_messages,
              (SELECT COUNT(*) FROM leads WHERE superseded_at IS NULL) AS leads,
              (SELECT value FROM settings WHERE key='raw_days') AS raw_days,
              (SELECT value FROM settings WHERE key='lead_days') AS lead_days,
              (SELECT value FROM settings WHERE key='diagnostic_days') AS diagnostic_days,
              (SELECT value FROM settings WHERE key='backup_keep') AS backup_keep"""
        ) or {}
        maintenance = {
            operation: self.maintenance.get(operation) or (
                json.loads(snapshot.get("maintenance_" + operation))
                if snapshot.get("maintenance_" + operation) else {"state": "unknown"}
            )
            for operation in ("cleanup", "backup")
        }
        usage = shutil.disk_usage(self.settings.data_dir)
        return {
            "accounts_connected": snapshot.get("connected") or 0,
            "last_received_at": snapshot.get("last_received_at"),
            "last_saved_at": snapshot.get("last_saved_at"),
            "maintenance": maintenance,
            "backlog": snapshot.get("backlog") or 0,
            "failed": snapshot.get("failed") or 0,
            "raw_messages": snapshot.get("raw_messages") or 0,
            "leads": snapshot.get("leads") or 0,
            "disk_free_bytes": usage.free,
            "data_path": str(self.settings.data_dir),
            "backup_path": str(self.settings.backup_dir),
            "worker_running": bool(self.worker_task and not self.worker_task.done()),
            "maintenance_running": bool(self.maintenance_task and not self.maintenance_task.done()),
            "retention": {
                "raw_days": int(snapshot.get("raw_days") or self.settings.raw_days),
                "lead_days": int(snapshot.get("lead_days") or self.settings.lead_days),
                "diagnostic_days": int(snapshot.get("diagnostic_days") or self.settings.diagnostic_days),
                "backup_keep": int(snapshot.get("backup_keep") or self.settings.backup_keep),
            },
        }
