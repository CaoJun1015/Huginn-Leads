from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from urllib.parse import quote
import uuid

from .database import Database, utcnow
from .rules import validate_conditions, validate_field_values

MAX_IMPORT_ROWS = 5000

DIAOHUO_QUERY = """SELECT
  UPPER(TRIM(
    p.series || '|' || COALESCE(p.cpu,'') || '|' || COALESCE(p.ram,'') || '|' ||
    COALESCE(p.storage,'') || '|' || COALESCE(p.gpu,'') || '|' || COALESCE(p.screen,'')
  )) AS external_key,
  TRIM(p.series || ' ' || COALESCE(p.cpu,'') || ' ' || COALESCE(p.ram,'') || ' ' ||
       COALESCE(p.storage,'') || ' ' || COALESCE(p.gpu,'')) AS product,
  CASE WHEN TRIM(p.series) LIKE '来酷%' AND LENGTH(TRIM(p.series)) > 2
       THEN SUBSTR(TRIM(p.series), 3) END AS weak_alias,
  p.series AS series, p.cpu AS cpu, p.gpu AS gpu, p.ram AS ram, p.storage AS storage, p.screen AS screen,
  SUM(b.remaining) AS available_quantity
FROM products p
JOIN batches b ON b.product_id=p.id AND b.deleted_at IS NULL
WHERE p.deleted_at IS NULL
GROUP BY external_key
HAVING SUM(b.remaining)>0"""


def external_presets() -> list[dict[str, Any]]:
    return [{
        "id": "diaohuo-sqlite",
        "name": "Diaohuo 电脑库存示例",
        "kind": "sqlite",
        "template_id": "diaohuo-stock",
        "query_text": DIAOHUO_QUERY,
        "key_column": "external_key",
        "field_mapping": {
            "product": "product", "series": "series", "cpu": "cpu", "gpu": "gpu", "ram": "ram",
            "storage": "storage", "available_quantity": "available_quantity", "screen": "screen",
        },
        "defaults": {"direction": "sell"},
        "name_fields": ["product"],
        "alias_fields": ["series"],
        "weak_alias_fields": ["weak_alias"],
        "alias_mode": "any",
    }]


class ExternalSourceError(ValueError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def source_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for source, target, default in (
        ("field_mapping_json", "field_mapping", {}),
        ("defaults_json", "defaults", {}),
        ("name_fields_json", "name_fields", []),
        ("alias_fields_json", "alias_fields", []),
        ("weak_alias_fields_json", "weak_alias_fields", []),
        ("task_source_ids_json", "task_source_ids", []),
    ):
        result[target] = _json(result.pop(source, None), default)
    return result


def validate_source_config(config: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    mapping = config.get("field_mapping") or {}
    defaults = config.get("defaults") or {}
    name_fields = list(dict.fromkeys(config.get("name_fields") or []))
    alias_fields = list(dict.fromkeys(config.get("alias_fields") or []))
    weak_alias_fields = list(dict.fromkeys(str(value).strip() for value in config.get("weak_alias_fields") or []))
    task_source_ids = list(dict.fromkeys(config.get("task_source_ids") or []))
    field_keys = {field["key"] for field in fields}
    if not isinstance(mapping, dict) or any(key not in field_keys or not str(value).strip() for key, value in mapping.items()):
        raise ExternalSourceError("invalid_mapping", "字段映射包含无效的业务字段或外部列名")
    if not isinstance(defaults, dict) or any(key not in field_keys for key in defaults):
        raise ExternalSourceError("invalid_defaults", "默认值包含无效的业务字段")
    available = set(mapping) | set(defaults)
    if not name_fields or any(key not in available for key in name_fields):
        raise ExternalSourceError("invalid_name_fields", "任务名称字段必须来自已映射字段或默认值")
    if not alias_fields or any(key not in available for key in alias_fields):
        raise ExternalSourceError("invalid_alias_fields", "触发关键词字段必须来自已映射字段或默认值")
    if any(not value for value in weak_alias_fields):
        raise ExternalSourceError("invalid_weak_alias_fields", "弱别名查询列名不能为空")
    if any(not isinstance(value, int) for value in task_source_ids):
        raise ExternalSourceError("invalid_task_sources", "任务来源格式无效")
    if config.get("alias_mode", "any") not in {"any", "all"}:
        raise ExternalSourceError("invalid_alias_mode", "关键词组合只能是 any 或 all")
    query = str(config.get("query_text") or "").strip()
    statement = query[:-1].rstrip() if query.endswith(";") else query
    if ";" in statement or not re.match(r"^(SELECT|WITH)\b", statement, re.I):
        raise ExternalSourceError("unsafe_query", "外部数据源只允许一条 SELECT 或 WITH 查询")
    if not str(config.get("key_column") or "").strip():
        raise ExternalSourceError("invalid_key_column", "必须指定外部唯一标识列")
    return {
        "field_mapping": {key: str(value).strip() for key, value in mapping.items()},
        "defaults": validate_field_values(defaults, [field for field in fields if field["key"] in defaults]),
        "name_fields": name_fields,
        "alias_fields": alias_fields,
        "weak_alias_fields": weak_alias_fields,
        "task_source_ids": task_source_ids,
        "alias_mode": config.get("alias_mode", "any"),
        "query_text": statement,
        "key_column": str(config["key_column"]).strip(),
    }


def _tokens(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    return [item.strip() for item in re.split(r"[,，\n]", str(value)) if item.strip()]


def _read_rows(path_value: str, query: str) -> list[dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ExternalSourceError("database_not_found", f"找不到外部数据库：{path}")
    uri = "file:" + quote(path.as_posix(), safe="/:\\") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        try:
            rows = connection.execute(query).fetchmany(MAX_IMPORT_ROWS + 1)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ExternalSourceError("external_query_failed", f"读取外部数据库失败：{exc}") from exc
    if len(rows) > MAX_IMPORT_ROWS:
        raise ExternalSourceError("too_many_rows", f"单次最多导入 {MAX_IMPORT_ROWS} 条记录，请收窄查询范围")
    return [dict(row) for row in rows]


def _prepare_rows(source: dict[str, Any], fields: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = validate_source_config(source, fields)
    field_types = {field["key"]: field["type"] for field in fields}
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        try:
            if config["key_column"] not in row:
                raise ValueError(f"查询结果缺少唯一标识列 {config['key_column']}")
            external_key = str(row[config["key_column"]] if row[config["key_column"]] is not None else "").strip()
            if not external_key or len(external_key) > 500:
                raise ValueError("外部唯一标识不能为空且最多 500 个字符")
            if external_key in seen:
                raise ValueError(f"外部唯一标识重复：{external_key}")
            raw_values = dict(config["defaults"])
            for field_key, column in config["field_mapping"].items():
                if column not in row:
                    raise ValueError(f"查询结果缺少已映射列 {column}")
                value = row[column]
                raw_values[field_key] = bool(value) if field_types[field_key] == "boolean" and value in (0, 1) else value
            values = validate_field_values(raw_values, fields)
            aliases = list(dict.fromkeys(token for key in config["alias_fields"] for token in _tokens(values.get(key))))
            if not aliases:
                raise ValueError("记录未生成任何触发关键词")
            strong = {re.sub(r"\s+", "", alias).lower() for alias in aliases}
            weak_aliases: list[str] = []
            for column in config["weak_alias_fields"]:
                if column not in row:
                    raise ValueError(f"查询结果缺少弱别名列 {column}")
                weak_aliases.extend(_tokens(row[column]))
            weak_aliases = list(dict.fromkeys(
                alias for alias in weak_aliases if re.sub(r"\s+", "", alias).lower() not in strong
            ))
            display_name = " ".join(str(values.get(key, "")).strip() for key in config["name_fields"] if values.get(key) not in (None, ""))
            if not display_name:
                raise ValueError("记录未生成任务名称")
            content = json.dumps(
                {"values": values, "aliases": aliases, "weak_aliases": weak_aliases, "display_name": display_name},
                ensure_ascii=False, sort_keys=True,
            )
            prepared.append({
                "external_key": external_key,
                "display_name": display_name[:120],
                "aliases": aliases,
                "weak_aliases": weak_aliases,
                "values": values,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            })
            seen.add(external_key)
        except ValueError as exc:
            if len(errors) < 20:
                errors.append({"row": index, "message": str(exc)})
    if errors:
        raise ExternalSourceError("invalid_external_rows", "外部记录不符合当前业务模式", errors)
    return prepared


class ExternalCatalog:
    def __init__(self, database: Database):
        self.database = database

    async def _load(self, source_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = await self.database.fetch_one("SELECT * FROM external_sources WHERE id=?", (source_id,))
        if not row:
            raise ExternalSourceError("external_source_not_found", "外部数据源不存在")
        source = source_view(row)
        revision = await self.database.fetch_one(
            "SELECT fields_json FROM template_revisions WHERE template_id=? AND version=?",
            (source["template_id"], source["template_version"]),
        )
        if not revision:
            raise ExternalSourceError("template_version_missing", "数据源关联的业务模式版本不存在")
        return source, _json(revision["fields_json"], [])

    async def _read(self, source_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        source, fields = await self._load(source_id)
        if source["status"] != "active":
            raise ExternalSourceError("external_source_archived", "外部数据源已停用")
        config = validate_source_config(source, fields)
        rows = await asyncio.to_thread(_read_rows, source["database_path"], config["query_text"])
        return source, fields, _prepare_rows(source, fields, rows)

    async def preview(self, source_id: str) -> dict[str, Any]:
        source, _, prepared = await self._read(source_id)
        current = {
            row["external_key"]: row
            for row in await self.database.fetch_all(
                "SELECT external_key,content_hash,active FROM external_records WHERE external_source_id=?",
                (source_id,),
            )
        }
        incoming = {row["external_key"]: row for row in prepared}
        return {
            "source_id": source_id,
            "total": len(prepared),
            "new": sum(key not in current for key in incoming),
            "changed": sum(key in current and current[key]["content_hash"] != value["content_hash"] for key, value in incoming.items()),
            "unchanged": sum(key in current and current[key]["content_hash"] == value["content_hash"] and current[key]["active"] for key, value in incoming.items()),
            "reactivated": sum(key in current and not current[key]["active"] for key in incoming),
            "missing": sum(key not in incoming and value["active"] for key, value in current.items()),
            "sample": prepared[:20],
            "template_id": source["template_id"],
            "template_version": source["template_version"],
        }

    async def import_records(self, source_id: str) -> dict[str, Any]:
        source, fields, prepared = await self._read(source_id)
        now = utcnow()
        for item in prepared:
            item["conditions"] = validate_conditions(
                {
                    "aliases": item["aliases"],
                    "weak_aliases": item["weak_aliases"],
                    "alias_mode": source["alias_mode"],
                    "source_ids": source["task_source_ids"],
                    "values": item["values"],
                },
                fields,
            )

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            before = {
                row["external_key"]: dict(row)
                for row in connection.execute(
                    "SELECT external_key,content_hash,active,task_id FROM external_records WHERE external_source_id=?",
                    (source_id,),
                )
            }
            incoming = {item["external_key"] for item in prepared}
            created = changed = reactivated = tasks_updated = 0
            for item in prepared:
                old = before.get(item["external_key"])
                created += old is None
                changed += bool(old and old["content_hash"] != item["content_hash"])
                reactivated += bool(old and not old["active"])
                connection.execute(
                    """INSERT INTO external_records(
                         id,external_source_id,external_key,template_id,template_version,display_name,
                         aliases_json,weak_aliases_json,values_json,content_hash,active,first_imported_at,last_seen_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)
                       ON CONFLICT(external_source_id,external_key) DO UPDATE SET
                         template_id=excluded.template_id,template_version=excluded.template_version,
                         display_name=excluded.display_name,aliases_json=excluded.aliases_json,
                         weak_aliases_json=excluded.weak_aliases_json,
                         values_json=excluded.values_json,content_hash=excluded.content_hash,
                         active=1,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at,
                         version=external_records.version+1""",
                    (
                        str(uuid.uuid4()), source_id, item["external_key"], source["template_id"],
                        source["template_version"], item["display_name"],
                        json.dumps(item["aliases"], ensure_ascii=False),
                        json.dumps(item["weak_aliases"], ensure_ascii=False),
                        json.dumps(item["values"], ensure_ascii=False), item["content_hash"], now, now, now,
                    ),
                )
                if old and old["task_id"] and old["content_hash"] != item["content_hash"] and source["task_source_ids"]:
                    task = connection.execute(
                        "SELECT current_revision FROM tasks WHERE id=?", (old["task_id"],)
                    ).fetchone()
                    if task:
                        revision = int(task["current_revision"]) + 1
                        connection.execute(
                            "INSERT INTO task_revisions(task_id,revision,template_version,conditions_json,created_at) VALUES (?,?,?,?,?)",
                            (old["task_id"], revision, str(source["template_version"]), json.dumps(item["conditions"], ensure_ascii=False), now),
                        )
                        connection.executemany(
                            "INSERT INTO task_revision_sources(task_id,revision,source_id) VALUES (?,?,?)",
                            [(old["task_id"], revision, value) for value in source["task_source_ids"]],
                        )
                        connection.execute(
                            "UPDATE tasks SET name=?,current_revision=?,updated_at=?,version=version+1 WHERE id=?",
                            (item["display_name"], revision, now, old["task_id"]),
                        )
                        connection.execute(
                            "INSERT INTO audit(occurred_at,object_type,object_id,action) VALUES (?,'task',?,'sync_external')",
                            (now, old["task_id"]),
                        )
                        tasks_updated += 1
            missing = [key for key, value in before.items() if value["active"] and key not in incoming]
            if missing:
                connection.executemany(
                    "UPDATE external_records SET active=0,updated_at=?,version=version+1 WHERE external_source_id=? AND external_key=?",
                    [(now, source_id, key) for key in missing],
                )
            connection.execute(
                "UPDATE external_sources SET last_imported_at=?,last_error=NULL,updated_at=?,version=version+1 WHERE id=?",
                (now, now, source_id),
            )
            connection.execute(
                "INSERT INTO audit(occurred_at,object_type,object_id,action) VALUES (?,'external_source',?,'import')",
                (now, source_id),
            )
            return {
                "total": len(prepared), "created": created, "changed": changed,
                "reactivated": reactivated, "deactivated": len(missing), "tasks_updated": tasks_updated,
            }

        return await self.database.write(write)

    async def publish(self, source_id: str) -> dict[str, Any]:
        source, fields = await self._load(source_id)
        if source["status"] != "active":
            raise ExternalSourceError("external_source_archived", "外部数据源已停用")
        template = await self.database.fetch_one("SELECT * FROM templates WHERE id=?", (source["template_id"],))
        if not template or template["status"] != "active" or int(template["current_version"]) != int(source["template_version"]):
            raise ExternalSourceError("template_version_conflict", "业务模式已经变化，请更新数据源配置后再发布")
        source_ids = source["task_source_ids"]
        if not source_ids:
            raise ExternalSourceError("task_sources_required", "发布任务前至少选择一个已启用的 QQ 来源")
        records = await self.database.fetch_all(
            "SELECT * FROM external_records WHERE external_source_id=? AND active=1 AND task_id IS NULL ORDER BY display_name",
            (source_id,),
        )
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for record in records:
            conditions = validate_conditions(
                {
                    "aliases": _json(record["aliases_json"], []),
                    "weak_aliases": _json(record["weak_aliases_json"], []),
                    "alias_mode": source["alias_mode"],
                    "source_ids": source_ids,
                    "values": _json(record["values_json"], {}),
                },
                fields,
            )
            prepared.append((record, conditions))
        now = utcnow()

        def write(connection: sqlite3.Connection) -> int:
            placeholders = ",".join("?" for _ in source_ids)
            enabled = connection.execute(
                f"SELECT COUNT(*) FROM sources WHERE enabled=1 AND id IN ({placeholders})", source_ids
            ).fetchone()[0]
            if enabled != len(source_ids):
                raise ExternalSourceError("invalid_task_sources", "任务来源已停用，请更新外部数据源配置")
            for record, conditions in prepared:
                task_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO tasks(id,name,template_id,status,current_revision,created_at,updated_at) VALUES (?,?,?,'active',1,?,?)",
                    (task_id, record["display_name"], source["template_id"], now, now),
                )
                connection.execute(
                    "INSERT INTO task_revisions(task_id,revision,template_version,conditions_json,created_at) VALUES (?,1,?,?,?)",
                    (task_id, str(source["template_version"]), json.dumps(conditions, ensure_ascii=False), now),
                )
                connection.executemany(
                    "INSERT INTO task_revision_sources(task_id,revision,source_id) VALUES (?,1,?)",
                    [(task_id, source_id_value) for source_id_value in source_ids],
                )
                connection.execute(
                    "UPDATE external_records SET task_id=?,updated_at=?,version=version+1 WHERE id=? AND task_id IS NULL",
                    (task_id, now, record["id"]),
                )
                connection.execute(
                    "INSERT INTO audit(occurred_at,object_type,object_id,action) VALUES (?,'task',?,'publish_external')",
                    (now, task_id),
                )
            return len(prepared)

        published = await self.database.write(write)
        return {"published": published, "skipped": max(0, len(records) - published)}

    async def records(self, source_id: str) -> list[dict[str, Any]]:
        await self._load(source_id)
        rows = await self.database.fetch_all(
            "SELECT * FROM external_records WHERE external_source_id=? ORDER BY active DESC,display_name",
            (source_id,),
        )
        for row in rows:
            row["aliases"] = _json(row.pop("aliases_json"), [])
            row["weak_aliases"] = _json(row.pop("weak_aliases_json"), [])
            row["values"] = _json(row.pop("values_json"), {})
        return rows
