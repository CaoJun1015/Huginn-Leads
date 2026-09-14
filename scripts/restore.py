from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import os
import shutil
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "huginn-leads.db"
BACKUPS = ROOT / "backups"


def integrity(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: restore.py <backup.db>")
        return 2
    source = Path(sys.argv[1])
    if not source.is_absolute():
        source = (ROOT / source).resolve()
    if not source.is_file() or source.suffix.lower() != ".db":
        print("Backup file not found or invalid.")
        return 2
    if not integrity(source):
        print("Backup failed SQLite integrity_check; database was not changed.")
        return 1
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    temp = DATABASE.with_suffix(".restore.tmp")
    shutil.copy2(source, temp)
    if not integrity(temp):
        temp.unlink(missing_ok=True)
        print("Copied backup failed validation; database was not changed.")
        return 1
    if DATABASE.exists():
        emergency = BACKUPS / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        current = sqlite3.connect(DATABASE)
        backup = sqlite3.connect(emergency)
        try:
            current.backup(backup)
        finally:
            backup.close()
            current.close()
    try:
        os.replace(temp, DATABASE)
    except PermissionError:
        temp.unlink(missing_ok=True)
        print("Database is in use. Stop Huginn Leads before restoring.")
        return 1
    for suffix in ("-wal", "-shm"):
        Path(str(DATABASE) + suffix).unlink(missing_ok=True)
    print(f"Restored {source.name}. Start Huginn Leads and verify the data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
