"""Offline, validated SQLite restore shared by source and frozen entry points."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import os
import sqlite3
import tempfile

from .database import _migration_sql
from .instance import InstanceLock

MIGRATIONS = Path(__file__).with_name('migrations')


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def integrity(path: Path) -> bool:
    try:
        with closing(readonly(path)) as connection:
            return connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    except sqlite3.Error:
        return False


def validate_backup(path: Path) -> int:
    with closing(readonly(path)) as source, closing(sqlite3.connect(':memory:')) as expected:
        if source.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('backup_corrupt')
        versions = [r[0] for r in source.execute('SELECT version FROM schema_migrations ORDER BY version')]
        scripts = sorted(MIGRATIONS.glob('*.sql'))
        if not versions or not all(type(v) is int for v in versions) or not 1 <= versions[-1] <= len(scripts) or versions != list(range(1, versions[-1] + 1)):
            raise ValueError('unsupported_schema_version')
        expected.execute("BEGIN")
        for script in scripts[:versions[-1]]:
            _migration_sql(expected, script.read_text(encoding='utf-8-sig'))
        for (table,) in expected.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            # Only names from our bundled migrations enter SQL here.
            for pragma in ('table_info', 'foreign_key_list'):
                if source.execute(f'PRAGMA {pragma}("{table}")').fetchall() != expected.execute(f'PRAGMA {pragma}("{table}")').fetchall():
                    raise ValueError('backup_schema_mismatch')
        for name, in expected.execute("SELECT name FROM sqlite_master WHERE type='index'"):
            if source.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is None:
                raise ValueError('backup_index_missing')
            if source.execute(f'PRAGMA index_info("{name}")').fetchall() != expected.execute(f'PRAGMA index_info("{name}")').fetchall():
                raise ValueError('backup_index_mismatch')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('backup_foreign_key_error')
        return versions[-1]


def copy_snapshot(source: Path, target: Path) -> None:
    with closing(readonly(source)) as current, closing(sqlite3.connect(target)) as backup:
        current.backup(backup)
        backup.execute("PRAGMA journal_mode=DELETE")


def restore_database(source: Path, database: Path, backups: Path) -> Path | None:
    source, database = source.resolve(), database.resolve()
    if not source.is_file() or source.suffix.lower() != '.db' or source == database:
        raise ValueError('invalid_backup_path')
    with InstanceLock(database):
        validate_backup(source)
        descriptor, name = tempfile.mkstemp(prefix='.restore-', suffix='.db', dir=database.parent)
        os.close(descriptor)
        staged = Path(name)
        try:
            copy_snapshot(source, staged)
            validate_backup(staged)
            emergency = None
            if database.exists():
                backups.mkdir(parents=True, exist_ok=True)
                emergency = backups / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
                copy_snapshot(database, emergency)
                if not integrity(emergency):
                    raise ValueError('current_database_backup_invalid')
                # Flush the old WAL before replacement. Conversion also refuses active
                # non-cooperating SQLite users; the OS lock guards application startup.
                with closing(sqlite3.connect(database, timeout=5)) as current:
                    if current.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                        raise RuntimeError('database_busy')
                    if current.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                        raise RuntimeError('database_busy')
            for suffix in ('-wal', '-shm'):
                Path(str(database) + suffix).unlink(missing_ok=True)
            os.replace(staged, database)
            return emergency
        finally:
            staged.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(staged) + suffix).unlink(missing_ok=True)


def main(argv: list[str], database: Path, backups: Path) -> int:
    if len(argv) != 1:
        print('Usage: HuginnLeads.exe restore <backup.db>  /  restore.cmd <backup.db>')
        return 2
    source = Path(argv[0])
    if not source.is_absolute():
        source = database.parent.parent / source
    try:
        emergency = restore_database(source, database, backups)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        # Only fixed codes/type names: no SQL, secrets or business data in output.
        code = str(exc).split(':', 1)[0] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(f'Restore refused/failed: {code}. Original database retained. Stop the application and verify the backup before retrying.')
        return 1
    if emergency:
        print(f'Previous database saved: {emergency.name}')
    print(f'Restored {source.name}. Start Huginn Leads and verify the data.')
    return 0
