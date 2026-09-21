import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from huginn_leads.config import Settings
from huginn_leads.database import Database, utcnow
from huginn_leads.external import ExternalCatalog, ExternalSourceError
from huginn_leads.service import Workbench


class ExternalCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            root=root, data_dir=root / "data", backup_dir=root / "backups",
            database_path=root / "data" / "huginn.db", static_dir=root / "static",
            origin="http://127.0.0.1:18080",
        )
        self.settings.data_dir.mkdir(); self.settings.backup_dir.mkdir()
        migrations = Path(__file__).resolve().parents[1] / "src" / "huginn_leads" / "migrations"
        self.db = Database(self.settings.database_path, migrations)
        await self.db.migrate()
        service = Workbench(self.db, self.settings)
        await service.connect_account("10001", "测试账号")
        await service.refresh_sources("10001", [
            {"group_id": 20001, "group_name": "测试群一"},
            {"group_id": 20002, "group_name": "测试群二"},
        ])
        self.sources = await self.db.fetch_all("SELECT id FROM sources ORDER BY id")
        await self.db.execute("UPDATE sources SET enabled=1")

        self.external_path = root / "只读库存.db"
        with closing(sqlite3.connect(self.external_path)) as connection:
            connection.execute("CREATE TABLE stock(code TEXT PRIMARY KEY,title TEXT NOT NULL,quantity INTEGER NOT NULL)")
            connection.execute("INSERT INTO stock VALUES ('a-1','品牌物品甲',3)")
            connection.commit()
        self.source_id = "external-1"
        now = utcnow()
        await self.db.execute(
            """INSERT INTO external_sources(
                 id,name,database_path,query_text,key_column,template_id,template_version,
                 field_mapping_json,defaults_json,name_fields_json,alias_fields_json,
                 weak_alias_fields_json,alias_mode,task_source_ids_json,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.source_id, "测试库存", str(self.external_path),
                "SELECT code,title,quantity FROM stock WHERE quantity>0", "code",
                "supply-demand", 1, json.dumps({"product": "title", "quantity": "quantity"}),
                json.dumps({"direction": "sell"}), json.dumps(["product"]), json.dumps(["product"]),
                "[]", "any", json.dumps([self.sources[0]["id"]]), now, now,
            ),
        )
        self.catalog = ExternalCatalog(self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_concurrent_publish_creates_one_task(self):
        await self.catalog.import_records(self.source_id)
        original_write = self.db.write
        ready = 0
        both_ready = asyncio.Event()

        async def synchronized_write(callback):
            nonlocal ready
            ready += 1
            if ready == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), 2)
            return await original_write(callback)

        with patch.object(self.db, "write", new=synchronized_write):
            results = await asyncio.gather(
                self.catalog.publish(self.source_id), self.catalog.publish(self.source_id)
            )
        count = await self.db.fetch_one("SELECT COUNT(*) AS value FROM tasks")
        self.assertEqual(count["value"], 1)
        self.assertEqual(sum(result["published"] for result in results), 1)

    async def test_repeat_import_and_source_only_change(self):
        first = await self.catalog.import_records(self.source_id)
        repeated = await self.catalog.import_records(self.source_id)
        self.assertEqual((first["created"], repeated["created"], repeated["changed"]), (1, 0, 0))
        await self.catalog.publish(self.source_id)
        record = await self.db.fetch_one("SELECT task_id FROM external_records")
        await self.db.execute(
            "UPDATE external_sources SET task_source_ids_json=?,version=version+1 WHERE id=?",
            (json.dumps([self.sources[1]["id"]]), self.source_id),
        )
        result = await self.catalog.import_records(self.source_id)
        task = await self.db.fetch_one("SELECT current_revision FROM tasks WHERE id=?", (record["task_id"],))
        linked = await self.db.fetch_all(
            "SELECT source_id FROM task_revision_sources WHERE task_id=? AND revision=?",
            (record["task_id"], task["current_revision"]),
        )
        self.assertEqual(result["tasks_updated"], 1)
        self.assertEqual((task["current_revision"], [row["source_id"] for row in linked]), (2, [self.sources[1]["id"]]))

    async def test_configuration_change_during_read_is_rejected(self):
        from huginn_leads import external

        started = threading.Event()
        release = threading.Event()
        original_read = external._read_rows

        def delayed_read(path, query):
            rows = original_read(path, query)
            started.set()
            release.wait(2)
            return rows

        with patch("huginn_leads.external._read_rows", side_effect=delayed_read):
            importing = asyncio.create_task(self.catalog.import_records(self.source_id))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await self.db.execute(
                "UPDATE external_sources SET alias_mode='all',version=version+1 WHERE id=?",
                (self.source_id,),
            )
            release.set()
            with self.assertRaises(ExternalSourceError) as caught:
                await importing
        self.assertEqual(caught.exception.code, "external_source_changed")
        count = await self.db.fetch_one("SELECT COUNT(*) AS value FROM external_records")
        self.assertEqual(count["value"], 0)

    async def test_failed_read_missing_and_reappearing_record_preserve_state_and_source(self):
        original_hash = hashlib.sha256(self.external_path.read_bytes()).hexdigest()
        await self.catalog.import_records(self.source_id)
        await self.catalog.publish(self.source_id)
        self.assertEqual(hashlib.sha256(self.external_path.read_bytes()).hexdigest(), original_hash)
        record = await self.db.fetch_one("SELECT id,task_id FROM external_records")
        await self.db.execute("UPDATE tasks SET status='paused' WHERE id=?", (record["task_id"],))

        await self.db.execute(
            "UPDATE external_sources SET query_text='SELECT * FROM missing_table',version=version+1 WHERE id=?",
            (self.source_id,),
        )
        with self.assertRaises(ExternalSourceError):
            await self.catalog.import_records(self.source_id)
        current = await self.db.fetch_one("SELECT active FROM external_records WHERE id=?", (record["id"],))
        self.assertEqual(current["active"], 1)

        await self.db.execute(
            "UPDATE external_sources SET query_text='SELECT code,title,quantity FROM stock WHERE quantity>0',version=version+1 WHERE id=?",
            (self.source_id,),
        )
        with closing(sqlite3.connect(self.external_path)) as connection:
            connection.execute("DELETE FROM stock")
            connection.commit()
        missing = await self.catalog.import_records(self.source_id)
        task = await self.db.fetch_one("SELECT status FROM tasks WHERE id=?", (record["task_id"],))
        current = await self.db.fetch_one("SELECT active FROM external_records WHERE id=?", (record["id"],))
        self.assertEqual((missing["deactivated"], current["active"], task["status"]), (1, 0, "paused"))

        with closing(sqlite3.connect(self.external_path)) as connection:
            connection.execute("INSERT INTO stock VALUES ('a-1','品牌物品甲',3)")
            connection.commit()
        restored = await self.catalog.import_records(self.source_id)
        task = await self.db.fetch_one("SELECT status FROM tasks WHERE id=?", (record["task_id"],))
        self.assertEqual((restored["reactivated"], task["status"]), (1, "paused"))


if __name__ == "__main__":
    unittest.main()
