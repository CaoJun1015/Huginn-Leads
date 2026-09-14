from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from huginn_leads.database import Database


async def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "benchmark.db"
        db = Database(path, ROOT / "src" / "huginn_leads" / "migrations")
        await db.migrate()
        now = datetime.now(timezone.utc)

        def seed(connection):
            connection.execute("INSERT INTO accounts(account_id,connected,updated_at) VALUES ('10001',1,?)", (now.isoformat(),))
            connection.execute("INSERT INTO sources(account_id,group_id,platform_name,enabled,updated_at) VALUES ('10001','20001','性能测试群',1,?)", (now.isoformat(),))
            rows = []
            for index in range(10_000):
                moment = (now - timedelta(seconds=index)).isoformat()
                rows.append((str(uuid.uuid4()), "qq", "10001", "20001", str(index), str(30000 + index % 100), "测试发布者", moment, moment, f"性能样本 ThinkBook {index}", "[]", (now + timedelta(days=7)).isoformat(), moment))
            connection.executemany("INSERT INTO raw_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        await db.write(seed)
        timings = []
        for _ in range(100):
            start = time.perf_counter()
            await db.fetch_all("SELECT id,text,received_at FROM raw_messages ORDER BY received_at DESC,id DESC LIMIT 50")
            timings.append((time.perf_counter() - start) * 1000)
        p95 = statistics.quantiles(timings, n=20)[18]
        print(f"10,000-row query P95: {p95:.2f} ms")
        assert p95 < 1000
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
