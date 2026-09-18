import asyncio
import threading
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from huginn_leads.config import Settings
from huginn_leads.database import Database, utcnow
from huginn_leads.rules import PROCESSOR_VERSION
from huginn_leads.security import Security
from huginn_leads.service import Workbench
from scripts import restore


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            root=root,
            data_dir=root / "data",
            backup_dir=root / "backups",
            database_path=root / "data" / "test.db",
            static_dir=root / "static",
            origin="http://127.0.0.1:18080",
        )
        self.settings.data_dir.mkdir()
        self.settings.backup_dir.mkdir()
        migrations = Path(__file__).resolve().parents[1] / "src" / "huginn_leads" / "migrations"
        self.db = Database(self.settings.database_path, migrations)
        await self.db.migrate()
        self.service = Workbench(self.db, self.settings)
        for key, value in {"raw_days": "7", "lead_days": "30", "diagnostic_days": "7", "backup_keep": "7"}.items():
            await self.db.set_setting(key, value)
        await self.service.connect_account("10001", "测试账号")
        await self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}])
        self.source = await self.db.fetch_one("SELECT * FROM sources WHERE account_id='10001'")
        await self.db.execute("UPDATE sources SET enabled=1 WHERE id=?", (self.source["id"],))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def create_task(self):
        conditions = {
            "direction": "purchase", "product": "办公本", "aliases": ["ThinkBook"], "alias_mode": "any",
            "specs": {"memory_gb": 16}, "price": {"value": 4500}, "quantity": {}, "trade": {},
            "source_ids": [self.source["id"]],
        }
        now = utcnow()
        await self.db.execute(
            "INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES ('task-1','采购办公本','supply-demand','active',1,?,?)",
            (now, now),
        )
        await self.db.execute(
            "INSERT INTO task_revisions(task_id,revision,template_version,conditions_json,created_at) VALUES ('task-1',1,'1',?,?)",
            (json.dumps(conditions, ensure_ascii=False), now),
        )
        await self.db.execute(
            "INSERT INTO task_revision_sources(task_id,revision,source_id) VALUES ('task-1',1,?)", (self.source["id"],)
        )

    def message(self, native_id="m-1", text="现货 ThinkBook 32G 1TB，4280元，2台"):
        return {
            "platform": "qq", "account_id": "10001", "group_id": "20001", "native_message_id": native_id,
            "sender_id": "30001", "sender_name": "测试发布者", "message_time": utcnow(), "text": text,
            "segments": [{"type": "text", "data": {"text": text}}],
        }

    async def test_disabled_source_is_not_saved(self):
        await self.db.execute("UPDATE sources SET enabled=0 WHERE id=?", (self.source["id"],))
        result = await self.service.ingest(self.message())
        self.assertFalse(result["saved"])
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM raw_messages"))["n"], 0)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM diagnostics"))["n"], 0)

    async def test_source_disable_or_disconnect_committed_after_precheck_rejects_all_payload(self):
        await self.create_task()
        other = Database(self.db.path, self.db.migration_dir)
        original_write = self.db.write
        for change in ("UPDATE sources SET enabled=0", "UPDATE accounts SET connected=0"):
            with self.subTest(change=change):
                await other.execute("UPDATE sources SET enabled=1")
                await other.execute("UPDATE accounts SET connected=1")

                async def close_before_transaction(callback):
                    # An independent connection commits after the fast precheck.
                    await other.execute(change)
                    return await original_write(callback)

                with patch.object(self.db, "write", close_before_transaction):
                    result = await self.service.ingest(self.message(text="unique-body-and-segments"))
                self.assertEqual(result, {"saved": False, "reason": "source_not_enabled"})
                for table in ("raw_messages", "processing_jobs"):
                    self.assertEqual((await self.db.fetch_one(f"SELECT COUNT(*) n FROM {table}"))["n"], 0)
                self.assertIsNone((await self.db.fetch_one("SELECT last_saved_at FROM sources"))["last_saved_at"])

    async def test_ingest_transaction_commits_before_disable_and_job_still_processes(self):
        await self.create_task()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_write = self.db.write
        other = Database(self.db.path, self.db.migration_dir)
        original_connect = other._connect

        def connect():
            connection = original_connect()
            connection.set_trace_callback(lambda sql: attempted.set() if sql.startswith("UPDATE sources") else None)
            return connection

        async def hold_before_commit(callback):
            def held(connection):
                result = callback(connection)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release ingest")
                return result
            return await original_write(held)

        pending = []
        try:
            with patch.object(self.db, "write", hold_before_commit), patch.object(other, "_connect", connect):
                ingestion = asyncio.create_task(self.service.ingest(self.message()))
                pending.append(ingestion)
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                closure = asyncio.create_task(other.execute("UPDATE sources SET enabled=0"))
                pending.append(closure)
                self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                self.assertFalse(closure.done())
                release.set()
                result, _ = await asyncio.gather(ingestion, closure)
        finally:
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
        self.assertTrue(result["saved"])
        self.assertEqual(result["jobs"], 1)
        self.assertFalse((await self.service.ingest(self.message("after-close")))["saved"])
        job = await self.service._claim_job()
        await self.service._process_job(job)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) n FROM leads"))["n"], 1)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) n FROM raw_messages"))["n"], 1)

    async def test_account_and_source_type_isolation_with_same_message_id(self):
        await self.create_task()
        await self.service.connect_account("10002", "second-synthetic-account")
        await self.service.refresh_sources("10002", [{"group_id": 20001}])
        await self.service.refresh_sources("10001", [{"user_id": 20001}], "contact")
        other_account = self.message() | {"account_id": "10002"}
        contact = self.message() | {"source_type": "contact", "peer_id": "20001"}
        for message in (other_account, contact):
            self.assertFalse((await self.service.ingest(message))["saved"])
        first = await self.service.ingest(self.message())
        self.assertEqual(first["jobs"], 1)
        await self.db.execute("UPDATE sources SET enabled=1")
        for message in (other_account, contact):
            result = await self.service.ingest(message)
            self.assertTrue(result["saved"])
            self.assertFalse(result["duplicate"])
            self.assertEqual(result["jobs"], 0)
            self.assertTrue((await self.service.ingest(message))["duplicate"])
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) n FROM raw_messages"))["n"], 3)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) n FROM processing_jobs"))["n"], 1)

    async def test_job_insert_failure_rolls_back_raw_and_segments(self):
        await self.create_task()
        await self.db.execute("CREATE TRIGGER fail_job BEFORE INSERT ON processing_jobs BEGIN SELECT RAISE(ABORT, 'synthetic-private-marker'); END")
        result = await self.service.ingest(self.message(text="synthetic-private-marker"))
        self.assertEqual(result, {"saved": False, "reason": "database_write_failed"})
        for table in ("raw_messages", "processing_jobs"):
            self.assertEqual((await self.db.fetch_one(f"SELECT COUNT(*) n FROM {table}"))["n"], 0)
        diagnostic = await self.db.fetch_one("SELECT * FROM diagnostics")
        self.assertEqual(diagnostic["code"], "database_write_failed")
        self.assertNotIn("synthetic-private-marker", str(diagnostic))

    async def test_enabled_contact_is_processed_like_group(self):
        await self.service.refresh_sources(
            "10001", [{"user_id": 30002, "nickname": "联系人甲", "remark": "采购甲"}], "contact"
        )
        contact = await self.db.fetch_one(
            "SELECT * FROM sources WHERE account_id='10001' AND source_type='contact'"
        )
        private = self.message(native_id="private-1") | {
            "source_type": "contact", "peer_id": "30002", "sender_id": "30002", "sender_name": "采购甲"
        }
        self.assertFalse((await self.service.ingest(private))["saved"])
        await self.db.execute("UPDATE sources SET enabled=1 WHERE id=?", (contact["id"],))
        await self.create_task()
        await self.db.execute("DELETE FROM task_revision_sources WHERE task_id='task-1'")
        await self.db.execute(
            "INSERT INTO task_revision_sources(task_id,revision,source_id) VALUES ('task-1',1,?)", (contact["id"],)
        )
        result = await self.service.ingest(private)
        self.assertEqual(result["jobs"], 1)
        job = await self.service._claim_job()
        await self.service._process_job(job)
        raw = await self.db.fetch_one("SELECT source_type, peer_id, source_id FROM raw_messages")
        self.assertEqual((raw["source_type"], raw["peer_id"], raw["source_id"]), ("contact", "30002", contact["id"]))
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM leads"))["n"], 1)

    async def test_duplicate_message_and_lead_are_idempotent(self):
        await self.create_task()
        first = await self.service.ingest(self.message())
        second = await self.service.ingest(self.message())
        self.assertTrue(second["duplicate"])
        job = await self.service._claim_job()
        await self.service._process_job(job)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM raw_messages"))["n"], 1)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM leads"))["n"], 1)
        await self.db.execute("UPDATE leads SET manual_status='contacted', note='已电话联系' WHERE task_id='task-1'")
        await self.db.execute("UPDATE processing_jobs SET status='pending', attempts=0, next_attempt_at=?", (utcnow(),))
        job = await self.service._claim_job()
        await self.service._process_job(job)
        lead = await self.db.fetch_one("SELECT * FROM leads")
        self.assertEqual(lead["manual_status"], "contacted")
        self.assertEqual(lead["note"], "已电话联系")
        self.assertEqual((lead["sender_id"], lead["sender_name"]), ("30001", "测试发布者"))
        self.assertEqual(first["jobs"], 1)

    async def test_storage_capacity_does_not_satisfy_memory_in_processing_job(self):
        await self.create_task()
        await self.service.ingest(self.message(text="现货 ThinkBook 512GB SSD"))
        await self.service._process_job(await self.service._claim_job())
        evaluation = await self.db.fetch_one(
            "SELECT processor_version,outcome,result_json FROM evaluations"
        )
        lead = await self.db.fetch_one("SELECT processor_version,outcome,extracted_json FROM leads")
        self.assertEqual(PROCESSOR_VERSION, "supply-demand/4")
        self.assertEqual((evaluation["processor_version"], evaluation["outcome"]), (PROCESSOR_VERSION, "review"))
        self.assertEqual((lead["processor_version"], lead["outcome"]), (PROCESSOR_VERSION, "review"))
        self.assertNotIn("memory_gb", json.loads(lead["extracted_json"])["specs"])

    async def test_two_tasks_share_raw_message(self):
        await self.create_task()
        now = utcnow()
        await self.db.execute(
            "INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES ('task-2','第二任务','supply-demand','active',1,?,?)", (now, now)
        )
        revision = await self.db.fetch_one("SELECT conditions_json FROM task_revisions WHERE task_id='task-1'")
        await self.db.execute("INSERT INTO task_revisions VALUES ('task-2',1,'1',?,?)", (revision["conditions_json"], now))
        await self.db.execute("INSERT INTO task_revision_sources VALUES ('task-2',1,?)", (self.source["id"],))
        result = await self.service.ingest(self.message())
        self.assertEqual(result["jobs"], 2)
        self.assertEqual((await self.db.fetch_one("SELECT COUNT(*) AS n FROM raw_messages"))["n"], 1)

    async def test_replay_reaches_completed_state(self):
        await self.create_task()
        await self.service.ingest(self.message())
        now = utcnow()
        await self.db.execute(
            """INSERT INTO replays(id,task_id,revision,start_at,end_at,status,eligible_count,created_at)
               VALUES ('replay-1','task-1',1,?,?,'running',1,?)""",
            (now, now, now),
        )
        await self.db.execute("UPDATE processing_jobs SET replay_id='replay-1'")
        job = await self.service._claim_job()
        await self.service._process_job(job)
        self.assertIsNone(await self.service._claim_job())
        replay = await self.db.fetch_one("SELECT status,completed_at FROM replays WHERE id='replay-1'")
        self.assertEqual(replay["status"], "completed")
        self.assertIsNotNone(replay["completed_at"])

    async def test_expired_raw_keeps_visible_job_state(self):
        await self.create_task()
        await self.service.ingest(self.message())
        await self.db.execute("UPDATE raw_messages SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        counts = await self.service.cleanup()
        self.assertEqual(counts["messages"], 1)
        job = await self.db.fetch_one("SELECT status, message_id FROM processing_jobs")
        self.assertEqual(job["status"], "source_expired")
        self.assertIsNone(job["message_id"])

    async def test_security_setup_and_session(self):
        security = Security(self.db)
        await security.start()
        code = security.setup_code
        await security.setup(code, "a-secure-local-password")
        token, csrf = await security.login("a-secure-local-password")
        session = await security.session(token)
        self.assertEqual(session["csrf_token"], csrf)
        await security.logout(token)
        self.assertIsNone(await security.session(token))

    async def test_consistent_backup(self):
        path = await self.service.backup()
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 0)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v1_group_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v1_migrations = root / "v1_migrations"
            v1_migrations.mkdir()
            all_migrations = Path(__file__).resolve().parents[1] / "src" / "huginn_leads" / "migrations"
            (v1_migrations / "001_initial.sql").write_text(
                (all_migrations / "001_initial.sql").read_text(encoding="utf-8"), encoding="utf-8"
            )
            path = root / "upgrade.db"
            old = Database(path, v1_migrations)
            await old.migrate()
            now = utcnow()
            await old.execute(
                "INSERT INTO accounts(account_id,platform_name,connected,updated_at) VALUES ('10001','旧账号',1,?)", (now,)
            )
            await old.execute(
                "INSERT INTO sources(id,account_id,group_id,platform_name,enabled,updated_at) VALUES (7,'10001','20001','旧群',1,?)", (now,)
            )
            await old.execute(
                "INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES ('old-task','旧任务','supply-demand','active',1,?,?)", (now, now)
            )
            await old.execute(
                "INSERT INTO task_revisions(task_id,revision,template_version,conditions_json,created_at) VALUES ('old-task',1,'1','{}',?)", (now,)
            )
            await old.execute("INSERT INTO task_revision_sources VALUES ('old-task',1,7)")
            await old.execute(
                """INSERT INTO raw_messages(id,platform,account_id,group_id,native_message_id,sender_id,message_time,received_at,text,segments_json,expires_at,created_at)
                   VALUES ('old-message','qq','10001','20001','m1','30001',?,?,?,?,?,?)""",
                (now, now, "旧消息", "[]", now, now),
            )
            upgraded = Database(path, all_migrations)
            await upgraded.migrate()
            source = await upgraded.fetch_one("SELECT * FROM sources WHERE id=7")
            message = await upgraded.fetch_one("SELECT * FROM raw_messages WHERE id='old-message'")
            self.assertEqual((source["source_type"], source["peer_id"]), ("group", "20001"))
            self.assertEqual(message["source_id"], 7)
            self.assertEqual((await upgraded.fetch_one("SELECT COUNT(*) AS n FROM tasks"))["n"], 1)
            self.assertEqual((await upgraded.fetch_one("SELECT name FROM templates WHERE id='supply-demand'"))["name"], "电脑经销商模式")


class RestoreTests(unittest.TestCase):
    def test_restore_replaces_database_and_keeps_emergency_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "huginn-leads.db"
            backups = root / "backups"
            snapshot = root / "snapshot.db"
            database.parent.mkdir()
            backups.mkdir()
            for path, value in ((database, "before"), (snapshot, "after")):
                connection = sqlite3.connect(path)
                try:
                    connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
                    connection.execute("INSERT INTO marker VALUES (?)", (value,))
                    connection.commit()
                finally:
                    connection.close()
            with (
                patch.object(restore, "DATABASE", database),
                patch.object(restore, "BACKUPS", backups),
                patch.object(sys, "argv", ["restore.py", str(snapshot)]),
            ):
                self.assertEqual(restore.main(), 0)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "after")
            finally:
                connection.close()
            emergency = next(backups.glob("before-restore-*.db"))
            connection = sqlite3.connect(emergency)
            try:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "before")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
