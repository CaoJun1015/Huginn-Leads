from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from huginn_leads.api import install_api
from huginn_leads.config import Settings
from huginn_leads.database import Database, utcnow
from huginn_leads.rules import PROCESSOR_VERSION
from huginn_leads.security import Security
from huginn_leads.service import Workbench


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
    }


def timed_requests(client: TestClient, path: str, repeats: int) -> dict[str, float]:
    response = client.get(path)
    response.raise_for_status()
    timings = []
    for _ in range(repeats):
        started = time.perf_counter()
        response = client.get(path)
        response.raise_for_status()
        timings.append((time.perf_counter() - started) * 1000)
    return distribution(timings)


def storage(path: Path) -> dict[str, int]:
    return {
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "wal_bytes": path.with_name(path.name + "-wal").stat().st_size if path.with_name(path.name + "-wal").exists() else 0,
    }


async def seed_tasks(database: Database, source_id: int, start: int, stop: int) -> None:
    now = utcnow()
    conditions = json.dumps({
        "aliases": ["BenchmarkModel"], "weak_aliases": [], "alias_mode": "any",
        "source_ids": [source_id], "values": {"direction": "sell", "product": "BenchmarkModel"},
    }, ensure_ascii=False)

    def write(connection: sqlite3.Connection) -> None:
        tasks = [(f"task-{i:04d}", f"性能任务 {i:04d}", "supply-demand", now, now) for i in range(start, stop)]
        connection.executemany(
            "INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES (?,?,?,'active',1,?,?)",
            tasks,
        )
        connection.executemany(
            "INSERT INTO task_revisions(task_id,revision,template_version,conditions_json,created_at) VALUES (?,1,'1',?,?)",
            [(row[0], conditions, now) for row in tasks],
        )
        connection.executemany(
            "INSERT INTO task_revision_sources(task_id,revision,source_id) VALUES (?,1,?)",
            [(row[0], source_id) for row in tasks],
        )

    await database.write(write)


def message(index: int) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return {
        "platform": "qq", "account_id": "10001", "source_type": "group", "peer_id": "20001",
        "native_message_id": f"fanout-{index}", "sender_id": "30001", "sender_name": "性能发布者",
        "message_time": now, "text": "现货 BenchmarkModel 内存16GB SSD512GB", "segments": [],
    }


async def fanout_and_drain(service: Workbench, database: Database, task_count: int, messages: int) -> dict[str, object]:
    ingest_ms = []
    started_batch = time.perf_counter()
    expected_jobs = 0
    for index in range(messages):
        started = time.perf_counter()
        result = await service.ingest(message(task_count * 100 + index))
        ingest_ms.append((time.perf_counter() - started) * 1000)
        expected_jobs += result["jobs"]
    ingest_seconds = time.perf_counter() - started_batch
    backlog = await database.fetch_one(
        "SELECT COUNT(*) AS value FROM processing_jobs WHERE status IN ('pending','deferred','processing')"
    )
    claim_ms = []
    process_ms = []
    drain_started = time.perf_counter()
    drain_limit = min(expected_jobs, 100)
    while len(process_ms) < drain_limit:
        started = time.perf_counter()
        job = await service._claim_job()
        claim_ms.append((time.perf_counter() - started) * 1000)
        if not job:
            break
        started = time.perf_counter()
        await service._process_job(job)
        process_ms.append((time.perf_counter() - started) * 1000)
    drain_seconds = time.perf_counter() - drain_started
    return {
        "tasks": task_count,
        "messages": messages,
        "jobs": expected_jobs,
        "backlog_peak": backlog["value"],
        "ingest": distribution(ingest_ms),
        "input_messages_per_second": round(messages / ingest_seconds, 2),
        "fanout_jobs_per_second": round(expected_jobs / ingest_seconds, 2),
        "claim": distribution(claim_ms),
        "processing": distribution(process_ms),
        "jobs_drained": len(process_ms),
        "backlog_remaining": backlog["value"] - len(process_ms),
        "drain_seconds": round(drain_seconds, 2),
        "drain_jobs_per_second": round(len(process_ms) / drain_seconds, 2),
    }


async def clear_runtime(database: Database) -> None:
    def write(connection: sqlite3.Connection) -> None:
        for table in ("leads", "evaluations", "processing_jobs", "raw_messages"):
            connection.execute(f"DELETE FROM {table}")
    await database.write(write)


async def seed_history(database: Database, source_id: int, start: int, stop: int, task_count: int) -> None:
    base = datetime.now(timezone.utc)
    chunk = 2_000
    for offset in range(start, stop, chunk):
        end = min(stop, offset + chunk)

        def write(connection: sqlite3.Connection, first=offset, last=end) -> None:
            raw = []
            jobs = []
            evaluations = []
            leads = []
            for index in range(first, last):
                stamp = (base - timedelta(seconds=stop - index)).isoformat(timespec="microseconds")
                expires = (base + timedelta(days=7)).isoformat(timespec="seconds")
                task_id = f"task-{index % task_count:04d}"
                message_id = f"history-message-{index:06d}"
                raw.append((
                    message_id, "qq", "10001", "20001", str(index), str(30000 + index % 100),
                    "性能发布者", stamp, stamp, f"现货 BenchmarkModel {index}", "[]", expires, stamp,
                    source_id, "group", "20001",
                ))
                jobs.append((f"history-job-{index:06d}", message_id, task_id, PROCESSOR_VERSION, stamp, stamp, stamp))
                evaluations.append((f"history-evaluation-{index:06d}", message_id, task_id, PROCESSOR_VERSION, stamp))
                leads.append((
                    f"history-lead-{index:06d}", task_id, message_id, f"item-{index}", PROCESSOR_VERSION,
                    '{"product":"BenchmarkModel"}', '{"text":"synthetic"}', "[]", expires, stamp, stamp,
                    "manual" if index % 20 == 0 else "machine", "人工保留" if index % 20 == 0 else "",
                ))
            connection.executemany(
                """INSERT INTO raw_messages(
                     id,platform,account_id,group_id,native_message_id,sender_id,sender_name,message_time,
                     received_at,text,segments_json,expires_at,created_at,source_id,source_type,peer_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", raw,
            )
            connection.executemany(
                """INSERT INTO processing_jobs(
                     id,message_id,task_id,revision,processor_version,status,next_attempt_at,created_at,updated_at
                   ) VALUES (?,?,?,1,?,'done',?,?,?)""", jobs,
            )
            connection.executemany(
                """INSERT INTO evaluations(
                     id,message_id,task_id,revision,processor_version,outcome,reason,result_json,processed_at
                   ) VALUES (?,?,?,1,?,'match','synthetic','[]',?)""", evaluations,
            )
            connection.executemany(
                """INSERT INTO leads(
                     id,task_id,message_id,item_key,revision,processor_version,direction,extracted_json,
                     evidence_json,comparison_json,outcome,reason,evidence_expires_at,created_at,updated_at,origin,note
                   ) VALUES (?,?,?,?,1,?,'supply',?,?,?,'match','synthetic',?,?,?,?,?)""", leads,
            )

        await database.write(write)


def api_metrics(client: TestClient, repeats: int) -> dict[str, dict[str, float]]:
    return {
        endpoint: timed_requests(client, f"/api/{endpoint}", repeats)
        for endpoint in ("messages", "leads", "tasks", "status")
    }


async def run(output: Path | None, repeats: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = Settings(
            root=root, data_dir=root / "data", backup_dir=root / "backups",
            database_path=root / "data" / "benchmark.db", static_dir=root / "static",
            origin="http://127.0.0.1:18080", max_active_jobs=20_000,
        )
        settings.data_dir.mkdir(); settings.backup_dir.mkdir()
        database = Database(settings.database_path, ROOT / "src" / "huginn_leads" / "migrations")
        await database.migrate()
        service = Workbench(database, settings)
        await service.connect_account("10001", "性能账号")
        await service.refresh_sources("10001", [{"group_id": 20001, "group_name": "性能测试群"}])
        source = await database.fetch_one("SELECT id FROM sources")
        await database.execute("UPDATE sources SET enabled=1 WHERE id=?", (source["id"],))

        security = Security(database)
        security.setup_code = "benchmark-setup-code"
        app = FastAPI()

        async def refresh_sources(_account):
            return {"results": [], "errors": []}

        async def validate_source(_account, _peer):
            return {}

        install_api(app, database, service, security, settings, refresh_sources, validate_source)
        client = TestClient(app, base_url=settings.origin)
        login = client.post(
            "/api/auth/setup", headers={"Origin": settings.origin},
            json={"code": security.setup_code, "password": "benchmark-secure-password"},
        )
        login.raise_for_status()

        report: dict[str, object] = {
            "generated_at": utcnow(),
            "environment": {
                "os": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
                "logical_cpus": os.cpu_count(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            },
            "method": {
                "database": "temporary SQLite WAL database", "data": "synthetic only",
                "api_repeats": repeats, "page_size": settings.page_size,
                "task_scales": [100, 1000], "history_scales": [10_000, 100_000],
            },
            "fanout": [], "api": {}, "storage": {},
        }

        print("phase: 100 tasks", file=sys.stderr, flush=True)
        await seed_tasks(database, source["id"], 0, 100)
        report["api"]["100_tasks_empty_history"] = api_metrics(client, repeats)
        report["fanout"].append(await fanout_and_drain(service, database, 100, 10))
        await clear_runtime(database)

        print("phase: 1000 tasks", file=sys.stderr, flush=True)
        await seed_tasks(database, source["id"], 100, 1000)
        report["api"]["1000_tasks_empty_history"] = api_metrics(client, repeats)
        report["fanout"].append(await fanout_and_drain(service, database, 1000, 3))
        await clear_runtime(database)

        print("phase: 10000 history", file=sys.stderr, flush=True)
        await seed_history(database, source["id"], 0, 10_000, 1000)
        report["api"]["10000_history"] = api_metrics(client, repeats)
        report["storage"]["10000_history"] = storage(settings.database_path)
        print("phase: 100000 history", file=sys.stderr, flush=True)
        await seed_history(database, source["id"], 10_000, 100_000, 1000)
        report["api"]["100000_history"] = api_metrics(client, repeats)
        report["storage"]["100000_history"] = storage(settings.database_path)

        counts = {}
        for table in ("tasks", "raw_messages", "processing_jobs", "evaluations", "leads"):
            row = await database.fetch_one(f"SELECT COUNT(*) AS value FROM {table}")
            counts[table] = row["value"]
        report["final_counts"] = counts
        report["manual_leads"] = (await database.fetch_one(
            "SELECT COUNT(*) AS value FROM leads WHERE origin='manual'"
        ))["value"]
        report["slowest_api_p95_ms"] = max(
            result["p95_ms"] for scale in report["api"].values() for result in scale.values()
        )
        report["target_ms"] = 1000
        report["target_met"] = report["slowest_api_p95_ms"] < report["target_ms"]
        client.close()

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Huginn Leads repeatable full-path performance benchmark")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    report = asyncio.run(run(args.output, max(5, args.repeats)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
