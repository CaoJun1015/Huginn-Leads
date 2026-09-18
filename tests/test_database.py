import asyncio
from contextlib import closing, redirect_stdout
import io
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from huginn_leads.database import Database, MigrationError
from scripts import restore

MIGRATIONS = Path(__file__).resolve().parents[1] / "src/huginn_leads/migrations"


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def migrations_until(self, root, version):
        directory = root / f"v{version}"
        directory.mkdir(exist_ok=True)
        for path in MIGRATIONS.glob("*.sql"):
            if int(path.name[:3]) <= version:
                shutil.copyfile(path, directory / path.name)
        return directory

    def dump(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return list(connection.iterdump())

    def backup(self, source, target):
        with closing(sqlite3.connect(source)) as connection, closing(sqlite3.connect(target)) as backup:
            connection.backup(backup)

    async def seed_v1(self, db):
        def seed(connection):
            connection.execute("INSERT INTO accounts(account_id,connected,updated_at) VALUES ('a',1,'2026-09-18')")
            connection.execute("INSERT INTO sources(id,account_id,group_id,enabled,updated_at) VALUES (1,'a','g',1,'2026-09-18')")
            connection.execute("INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES ('t','synthetic','supply-demand','active',1,'now','now')")
            connection.execute("INSERT INTO task_revisions VALUES ('t',1,'1','{}','now')")
            connection.execute("INSERT INTO task_revision_sources VALUES ('t',1,1)")
            connection.execute("INSERT INTO raw_messages VALUES ('m','qq','a','g','native','sender','synthetic-name','now','now','synthetic-body','[]','future','now')")
            connection.execute("INSERT INTO leads(id,task_id,message_id,item_key,revision,processor_version,direction,extracted_json,evidence_json,comparison_json,outcome,reason,manual_status,manual_overrides_json,note,seen_at,evidence_expires_at,created_at,updated_at) VALUES ('l','t','m','manual-l',1,'v1','supply','{}','{}','{}','review','manual','contacted','{\"price\":123}','follow-up','seen','future','now','now')")
        await db.write(seed)

    async def assert_preserved(self, db):
        lead = await db.fetch_one("SELECT * FROM leads WHERE id='l'")
        self.assertEqual((lead["manual_status"], lead["manual_overrides_json"], lead["note"], lead["seen_at"]),
                         ("contacted", '{"price":123}', "follow-up", "seen"))
        self.assertEqual((lead["sender_id"], lead["sender_name"]), ("sender", "synthetic-name"))
        self.assertEqual((await db.fetch_one("SELECT source_id FROM raw_messages"))["source_id"], 1)
        for table in ("sources", "tasks", "task_revisions", "task_revision_sources", "raw_messages", "leads"):
            self.assertEqual((await db.fetch_one(f"SELECT COUNT(*) n FROM {table}"))["n"], 1)
        self.assertEqual(await db.fetch_all("PRAGMA foreign_key_check"), [])
        self.assertTrue(await db.integrity_check())

    async def test_each_version_rolls_back_schema_data_and_registration_then_retries(self):
        for version in range(1, 7):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "test.db"
                old = Database(path, self.migrations_until(root, min(1, version - 1)))
                await old.migrate()
                if version > 1:
                    await self.seed_v1(old)
                    await Database(path, self.migrations_until(root, version - 1)).migrate()
                # The real ledger INSERT fails after all schema and data statements.
                await old.execute(f"""CREATE TRIGGER fail_registration BEFORE INSERT ON schema_migrations
                    WHEN NEW.version={version} BEGIN SELECT RAISE(ABORT, 'synthetic-private-marker'); END""")
                before = self.dump(path)
                db = Database(path, MIGRATIONS)
                with self.assertRaises(MigrationError) as caught:
                    await db.migrate()
                self.assertIn(f"version={version}", str(caught.exception))
                self.assertNotIn("synthetic-private-marker", str(caught.exception))
                self.assertEqual(self.dump(path), before)
                await db.execute("DROP TRIGGER fail_registration")
                await db.migrate()
                self.assertEqual([row["version"] for row in await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")], [1, 2, 3, 4, 5, 6])
                before_restart = self.dump(path)
                await db.migrate()
                self.assertEqual(self.dump(path), before_restart)
                self.assertTrue(await db.integrity_check())
                self.assertEqual(await db.fetch_all("PRAGMA foreign_key_check"), [])
                if version > 1:
                    await self.assert_preserved(db)

    async def test_v5_provenance_backfill_manual_markers_and_unknown_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.db", self.migrations_until(root, 1))
            await db.migrate()
            await self.seed_v1(db)
            await Database(db.path, self.migrations_until(root, 5)).migrate()
            await db.execute("UPDATE raw_messages SET message_time='2020-01-02T03:04:05+00:00', received_at='2026-09-18T00:00:00+00:00'")
            await db.execute("UPDATE leads SET superseded_at='old-replay'")
            # Prefix, JSON-only marker, ordinary machine, and expired unknown history.
            columns = [r["name"] for r in await db.fetch_all("PRAGMA table_info(leads)")]
            original = await db.fetch_one("SELECT * FROM leads WHERE id='l'")
            for identifier, evidence, message in (("json-manual", '{"text":"kept","manual":true}', "m"),
                                                   ("machine", '{"text":"machine"}', "m"),
                                                   ("unknown", '{"text":"history","manual":true}', None)):
                row = original | {"id": identifier, "item_key": identifier, "evidence_json": evidence, "message_id": message}
                await db.execute(f"INSERT INTO leads({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[c] for c in columns])
            upgraded = Database(db.path, MIGRATIONS)
            await upgraded.migrate()
            for row in await upgraded.fetch_all("SELECT * FROM leads"):
                self.assertEqual(row["origin"], "machine" if row["id"] == "machine" else "manual")
                self.assertEqual(row["superseded_at"], "old-replay" if row["id"] == "machine" else None)
                self.assertEqual((row["note"], row["manual_status"], row["manual_overrides_json"], row["seen_at"]),
                                 ("follow-up", "contacted", '{"price":123}', "seen"))
                if row["id"] == "unknown":
                    self.assertTrue(all(row[c] is None for c in ("source_id", "account_id", "platform", "message_time", "received_at")))
                else:
                    self.assertEqual((row["platform"], row["account_id"], row["source_id"], row["source_type"], row["peer_id"], row["source_name"], row["native_message_id"]),
                                     ("qq", "a", 1, "group", "g", "g", "native"))
                    self.assertEqual(row["message_time"], "2020-01-02T03:04:05+00:00")
                    self.assertEqual(row["received_at"], "2026-09-18T00:00:00+00:00")
            before = self.dump(db.path)
            await upgraded.migrate()
            self.assertEqual(self.dump(db.path), before)
            self.assertEqual(await upgraded.fetch_all("PRAGMA foreign_key_check"), [])
            self.assertTrue(await upgraded.integrity_check())

    async def test_independent_runners_recheck_version_under_database_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            first, second = Database(path, MIGRATIONS), Database(path, MIGRATIONS)
            await asyncio.gather(first.migrate(), second.migrate())
            self.assertEqual([r["version"] for r in await first.fetch_all("SELECT version FROM schema_migrations ORDER BY version")], [1, 2, 3, 4, 5, 6])
            self.assertTrue(await first.integrity_check())

    async def test_script_cannot_commit_runner_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "migrations"
            scripts.mkdir()
            for control in ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT partial"):
                with self.subTest(control=control):
                    (scripts / "001_test.sql").write_text(f"CREATE TABLE partial(value); {control};", encoding="utf-8")
                    db = Database(root / "test.db", scripts)
                    with self.assertRaises(MigrationError):
                        await db.migrate()
                    self.assertIsNone(await db.fetch_one("SELECT name FROM sqlite_master WHERE name='partial'"))
                    self.assertEqual(await db.fetch_all("SELECT * FROM schema_migrations"), [])
            # Semicolons in strings/comments and a multi-statement trigger are valid SQL.
            (scripts / "001_test.sql").write_text("""-- comment;
CREATE TABLE partial(value);
                CREATE TRIGGER fill AFTER INSERT ON partial WHEN NEW.value='a;b'
                BEGIN INSERT INTO partial VALUES ('c;d'); INSERT INTO partial VALUES ('e'); END;
                INSERT INTO partial VALUES ('a;b'); -- trailing comment""", encoding="utf-8")
            await db.migrate()
            self.assertEqual([r["value"] for r in await db.fetch_all("SELECT * FROM partial")], ["a;b", "c;d", "e"])

    async def test_legacy_half_applied_v4_is_rejected_and_verified_backup_can_recover(self):
        for state in ("partial", "full", "v5_gap"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "test.db"
                db = Database(path, self.migrations_until(root, 1))
                await db.migrate()
                await self.seed_v1(db)
                await Database(path, self.migrations_until(root, 3)).migrate()
                snapshot = root / "before-upgrade.db"
                self.backup(path, snapshot)
                # Partial DDL, fully applied v4, and the original F5 ledger gap at v5.
                with closing(sqlite3.connect(path)) as connection:
                    sql = (MIGRATIONS / "004_lead_contacts.sql").read_text(encoding="utf-8")
                    connection.executescript(sql.split(";", 1)[0] + ";" if state == "partial" else sql)
                    if state == "v5_gap":
                        connection.executescript((MIGRATIONS / "005_weak_aliases.sql").read_text(encoding="utf-8"))
                        connection.execute("INSERT INTO schema_migrations VALUES (5,'synthetic')")
                        connection.commit()
                broken_state = self.dump(path)
                upgraded = Database(path, MIGRATIONS)
                with self.assertRaisesRegex(MigrationError, "version=4.*SQLITE_ERROR"):
                    await upgraded.migrate()
                self.assertEqual(self.dump(path), broken_state)
                # Documented route: preserve a SQLite backup first; inspect only metadata/counts.
                damaged_copy = root / "damaged.db"
                self.backup(path, damaged_copy)
                self.assertEqual(self.dump(damaged_copy), broken_state)
                self.assertEqual([r["version"] for r in await upgraded.fetch_all("SELECT version FROM schema_migrations")], [1, 2, 3, 5] if state == "v5_gap" else [1, 2, 3])
                self.assertIn("sender_id", [r["name"] for r in await upgraded.fetch_all("PRAGMA table_info(leads)")])
                self.assertTrue(restore.integrity(snapshot))
                with patch.object(restore, "DATABASE", path), patch.object(restore, "BACKUPS", root / "backups"), patch.object(sys, "argv", ["restore.py", str(snapshot)]), redirect_stdout(io.StringIO()):
                    self.assertEqual(restore.main(), 0)
                await upgraded.migrate()
                await self.assert_preserved(upgraded)
                before_restart = self.dump(path)
                await upgraded.migrate()
                self.assertEqual(self.dump(path), before_restart)
                self.assertEqual(self.dump(damaged_copy), broken_state)


if __name__ == "__main__":
    unittest.main()
