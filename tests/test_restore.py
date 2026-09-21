import asyncio
from contextlib import closing
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from huginn_leads.database import Database
from huginn_leads.instance import InstanceLock
from huginn_leads.restore import MIGRATIONS, restore_database, validate_backup, main


class RestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='恢复 中文 # 空格 ')
        self.root = Path(self.temp.name)
        self.database = self.root / 'data' / 'huginn-leads.db'
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        self.db = Database(self.database, MIGRATIONS)
        asyncio.run(self.db.migrate())
        asyncio.run(self.db.set_setting('synthetic_marker', 'before'))
        self.snapshot = self.root / '旧备份 文件.db'
        with closing(sqlite3.connect(self.database)) as source, closing(sqlite3.connect(self.snapshot)) as target:
            source.backup(target)
        asyncio.run(self.db.set_setting('synthetic_marker', 'after'))

    def tearDown(self):
        self.temp.cleanup()

    def marker(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("SELECT value FROM settings WHERE key='synthetic_marker'").fetchone()[0]

    def test_restore_preserves_current_copy_and_unique_names(self):
        emergency = restore_database(self.snapshot, self.database, self.backups)
        self.assertEqual(self.marker(self.database), 'before')
        self.assertEqual(self.marker(emergency), 'after')
        second = restore_database(self.snapshot, self.database, self.backups)
        self.assertNotEqual(emergency, second)
        self.assertEqual(self.marker(emergency), 'after')
        self.assertFalse(Path(str(self.database) + '-wal').exists())
        self.assertFalse(Path(str(self.database) + '-shm').exists())

    def test_refuse_corrupt_unrelated_future_and_half_applied_backups(self):
        corrupt = self.root / 'corrupt.db'
        corrupt.write_bytes(b'not SQLite')
        unrelated = self.root / 'unrelated.db'
        with closing(sqlite3.connect(unrelated)) as connection:
            connection.execute('CREATE TABLE marker(value)')
        future = self.root / 'future.db'
        shutil.copyfile(self.snapshot, future)
        with closing(sqlite3.connect(future)) as connection:
            connection.execute("INSERT INTO schema_migrations VALUES (999999999,'synthetic')")
            connection.commit()
        partial = self.root / 'partial.db'
        shutil.copyfile(self.snapshot, partial)
        with closing(sqlite3.connect(partial)) as connection:
            connection.execute('DELETE FROM schema_migrations WHERE version=6')
            connection.commit()
        for path in (corrupt, unrelated, future, partial, self.root / 'missing.db', self.database):
            with self.subTest(path=path.name):
                self.assertEqual(main([str(path)], self.database, self.backups), 1)
                self.assertEqual(self.marker(self.database), 'after')
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(main([], self.database, self.backups), 2)
        self.assertEqual(main(['a', 'b'], self.database, self.backups), 2)

    def test_replace_failure_preserves_database_and_emergency_copy(self):
        with patch('huginn_leads.restore.os.replace', side_effect=PermissionError('synthetic')):
            self.assertEqual(main([str(self.snapshot)], self.database, self.backups), 1)
        self.assertEqual(self.marker(self.database), 'after')
        self.assertEqual(self.marker(next(self.backups.glob('before-restore-*.db'))), 'after')
        self.assertEqual(list(self.database.parent.glob('.restore-*')), [])
        self.assertIsNotNone(restore_database(self.snapshot, self.database, self.backups))

    def test_all_supported_old_versions_restore_and_upgrade(self):
        for version in range(1, 7):
            with self.subTest(version=version):
                scripts = self.root / f'v{version}'
                scripts.mkdir()
                for path in sorted(MIGRATIONS.glob('*.sql'))[:version]:
                    shutil.copyfile(path, scripts / path.name)
                old = self.root / f'old-{version}.db'
                database = Database(old, scripts)
                asyncio.run(database.migrate())
                asyncio.run(database.set_setting('synthetic_marker', f'v{version}'))
                self.assertEqual(validate_backup(old), version)
                restore_database(old, self.database, self.backups)
                self.assertEqual(validate_backup(self.database), version)
                asyncio.run(self.db.migrate())
                self.assertEqual(validate_backup(self.database), 7)
                self.assertEqual(self.marker(self.database), f'v{version}')

    def test_os_lock_rejects_running_restore_and_is_released_after_process_kill(self):
        command = 'from pathlib import Path; import sys,time; from huginn_leads.instance import InstanceLock; lock=InstanceLock(Path(sys.argv[1])); lock.acquire(); print("locked",flush=True); time.sleep(60)'
        process = subprocess.Popen([sys.executable, '-c', command, str(self.database)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            self.assertEqual(main([str(self.snapshot)], self.database, self.backups), 1)
            self.assertEqual(self.marker(self.database), 'after')
        finally:
            process.kill()
            process.communicate(timeout=10)
        self.assertEqual(main([str(self.snapshot)], self.database, self.backups), 0)

    def test_source_backup_includes_wal_and_old_wal_does_not_contaminate_restore(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("UPDATE settings SET value='wal-state' WHERE key='synthetic_marker'")
            connection.commit()
            # An active external SQLite user is also refused, even without our app lock.
            connection.execute('BEGIN IMMEDIATE')
            with self.assertRaises((RuntimeError, sqlite3.Error)):
                restore_database(self.snapshot, self.database, self.backups)
            connection.rollback()
        finally:
            connection.close()
        emergency = restore_database(self.snapshot, self.database, self.backups)
        self.assertEqual(self.marker(emergency), 'wal-state')
        self.assertEqual(self.marker(self.database), 'before')


if __name__ == '__main__':
    unittest.main()
