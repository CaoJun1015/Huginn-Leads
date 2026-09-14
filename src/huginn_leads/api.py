from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import sqlite3
import uuid
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .database import Database, utcnow
from .rules import DEFAULT_TEMPLATE_FIELDS, PROCESSOR_VERSION, validate_conditions, validate_template_fields
from .security import Security
from .service import Workbench

COOKIE = "huginn_session"


class SetupBody(BaseModel):
    code: str
    password: str


class LoginBody(BaseModel):
    password: str


class AccountPatch(BaseModel):
    user_alias: str | None = Field(default=None, max_length=80)
    version: int


class SourcePatch(BaseModel):
    enabled: bool | None = None
    user_alias: str | None = Field(default=None, max_length=120)
    version: int


class ManualSource(BaseModel):
    account_id: str
    group_id: str


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    template_id: str = "supply-demand"
    template_version: int | None = None
    conditions: dict[str, Any]
    expires_at: str | None = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    fields: list[dict[str, Any]] = Field(default_factory=list)


class TemplatePatch(TemplateCreate):
    version: int


class OnboardingPatch(BaseModel):
    action: str


class LeadPatch(BaseModel):
    manual_status: str | None = None
    note: str | None = Field(default=None, max_length=4000)
    manual_overrides: dict[str, Any] | None = None
    seen: bool | None = None
    version: int


class ReplayBody(BaseModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("重跑时间必须包含时区")
        if self.start_at >= self.end_at:
            raise ValueError("重跑结束时间必须晚于开始时间")
        return self


class RetentionPatch(BaseModel):
    raw_days: int = Field(ge=1, le=365)
    lead_days: int = Field(ge=1, le=3650)
    diagnostic_days: int = Field(ge=1, le=365)
    backup_keep: int = Field(ge=1, le=100)
    confirmation_token: str | None = None


def _error(status: int, code: str, message: str, details: Any = None) -> HTTPException:
    content: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        content["details"] = details
    return HTTPException(status_code=status, detail=content)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _cursor(timestamp: str, identifier: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([timestamp, identifier]).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return str(decoded[0]), str(decoded[1])
    except Exception as exc:
        raise _error(400, "invalid_cursor", "分页游标无效") from exc


def install_api(
    app: FastAPI,
    database: Database,
    service: Workbench,
    security: Security,
    settings: Settings,
    refresh_sources: Callable[[str | None], Awaitable[dict[str, Any]]],
    validate_source: Callable[[str, str], Awaitable[dict[str, Any]]],
    probe_connections: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    router = APIRouter(prefix="/api")
    confirmations: dict[str, tuple[str, datetime]] = {}

    def issue_confirmation(scope: str) -> str:
        token = secrets.token_urlsafe(24)
        confirmations[token] = (scope, datetime.now(timezone.utc) + timedelta(minutes=10))
        return token

    def consume_confirmation(token: str | None, scope: str) -> bool:
        if not token:
            return False
        value = confirmations.pop(token, None)
        return bool(value and value[0] == scope and value[1] > datetime.now(timezone.utc))

    def check_origin(request: Request) -> None:
        if request.headers.get("origin") != settings.origin:
            raise _error(403, "origin_rejected", "请求来源无效")

    async def require_session(request: Request) -> dict[str, str]:
        session = await security.session(request.cookies.get(COOKIE))
        if not session:
            raise _error(401, "authentication_required", "请先登录")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            check_origin(request)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf_token"]):
                raise _error(403, "csrf_rejected", "安全令牌无效，请刷新页面后重试")
        return session

    def limit_value(limit: int) -> int:
        return min(max(limit, 1), settings.max_page_size)

    def values_from_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
        if "values" in conditions:
            return conditions.get("values") or {}
        specs = conditions.get("specs") or {}
        return {
            key: value for key, value in {
                "direction": conditions.get("direction"), "product": conditions.get("product"),
                "cpu": specs.get("cpu"), "gpu": specs.get("gpu"), "memory_gb": specs.get("memory_gb"),
                "storage_tb": specs.get("storage_tb"), "price": (conditions.get("price") or {}).get("value"),
                "quantity": (conditions.get("quantity") or {}).get("value"),
                "quantity_unit": (conditions.get("quantity") or {}).get("unit"),
                "region": (conditions.get("trade") or {}).get("region"),
            }.items() if value not in (None, "")
        }

    async def template_revision(template_id: str, version: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        template = await database.fetch_one("SELECT * FROM templates WHERE id=?", (template_id,))
        if not template:
            raise _error(404, "template_not_found", "业务模式不存在")
        selected = int(version or template["current_version"])
        revision = await database.fetch_one(
            "SELECT fields_json FROM template_revisions WHERE template_id=? AND version=?", (template_id, selected)
        )
        if not revision:
            raise _error(409, "template_version_conflict", "业务模式版本已变化，请刷新后重试")
        return template, _json(revision["fields_json"], [])

    async def build_status() -> dict[str, Any]:
        status = await service.status()
        database_ok = await database.integrity_check()
        now = utcnow()
        internal_ok = status["worker_running"] and status["maintenance_running"]
        try:
            external = await asyncio.wait_for(probe_connections(), timeout=5) if probe_connections else {
                "napcatqq": {"state": "warning", "message": "当前环境未启用连接探测"},
                "qq": {"state": "warning", "message": "当前环境未启用登录探测", "accounts": []},
            }
        except Exception:
            external = {
                "napcatqq": {"state": "error", "message": "NapCatQQ 状态检测失败"},
                "qq": {"state": "error", "message": "QQ 登录状态检测失败", "accounts": []},
            }
        status["components"] = {
            "huginn_leads": {
                "state": "ok" if database_ok and internal_ok else "warning",
                "message": "API、数据库和后台任务正常" if database_ok and internal_ok else "API 可用，但后台任务尚未全部运行",
                "checked_at": now,
            },
            "nonebot2": {"state": "ok", "message": "NoneBot2 驱动正在响应", "checked_at": now},
            "napcatqq": external["napcatqq"] | {"checked_at": now},
            "qq": external["qq"] | {"checked_at": now},
        }
        status["database_ok"] = database_ok
        status["gaps"] = await database.fetch_all("SELECT * FROM connection_gaps ORDER BY started_at DESC LIMIT 20")
        return status

    @app.exception_handler(HTTPException)
    async def api_http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api"):
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
            return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def api_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api"):
            return JSONResponse(
                status_code=422,
                content={"code": "validation_error", "message": "提交内容格式不正确", "details": jsonable_encoder(exc.errors())},
            )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @router.get("/auth/status")
    async def auth_status(request: Request):
        initialized = await security.is_initialized()
        session = await security.session(request.cookies.get(COOKIE))
        return {"initialized": initialized, "authenticated": bool(session), "csrf_token": session["csrf_token"] if session else None}

    @router.post("/auth/setup")
    async def auth_setup(body: SetupBody, request: Request):
        check_origin(request)
        try:
            await security.setup(body.code, body.password)
            token, csrf = await security.login(body.password)
        except ValueError as exc:
            raise _error(400, "setup_failed", str(exc)) from exc
        response = JSONResponse({"ok": True, "csrf_token": csrf})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=False, max_age=86400, path="/")
        return response

    @router.post("/auth/login")
    async def auth_login(body: LoginBody, request: Request):
        check_origin(request)
        try:
            token, csrf = await security.login(body.password)
        except ValueError as exc:
            raise _error(401, "login_failed", str(exc)) from exc
        response = JSONResponse({"ok": True, "csrf_token": csrf})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=False, max_age=86400, path="/")
        return response

    @router.post("/auth/logout")
    async def auth_logout(request: Request, _session=Depends(require_session)):
        await security.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @router.get("/status")
    async def get_status(_session=Depends(require_session)):
        return await build_status()

    async def onboarding_payload() -> dict[str, Any]:
        status = await build_status()
        return {
            "seen": await database.get_setting("onboarding_seen") == "1",
            "completed": await database.get_setting("onboarding_completed") == "1",
            "websocket_url": settings.origin.replace("http://", "ws://", 1) + "/onebot/v11/ws",
            "access_token_configured": settings.onebot_token_configured,
            "components": status["components"],
            "enabled_sources": (await database.fetch_one("SELECT COUNT(*) AS value FROM sources WHERE enabled=1") or {"value": 0})["value"],
        }

    @router.get("/onboarding")
    async def get_onboarding(_session=Depends(require_session)):
        return await onboarding_payload()

    @router.post("/onboarding/check")
    async def check_onboarding(_session=Depends(require_session)):
        return await onboarding_payload()

    @router.patch("/onboarding")
    async def patch_onboarding(body: OnboardingPatch, _session=Depends(require_session)):
        if body.action not in {"dismiss", "complete"}:
            raise _error(400, "invalid_onboarding_action", "配置向导操作无效")
        await database.set_setting("onboarding_seen", "1")
        if body.action == "complete":
            payload = await onboarding_payload()
            required = ("huginn_leads", "nonebot2", "napcatqq", "qq")
            if any(payload["components"][key]["state"] != "ok" for key in required) or not payload["enabled_sources"]:
                raise _error(400, "onboarding_incomplete", "请先完成连接检测并启用至少一个来源")
            await database.set_setting("onboarding_completed", "1")
        return await onboarding_payload()

    @router.get("/accounts")
    async def get_accounts(_session=Depends(require_session)):
        return {"items": await database.fetch_all("SELECT * FROM accounts ORDER BY connected DESC, account_id")}

    @router.patch("/accounts/{account_id}")
    async def patch_account(account_id: str, body: AccountPatch, _session=Depends(require_session)):
        changed = await database.execute(
            """UPDATE accounts SET user_alias=?, updated_at=?, version=version+1
               WHERE account_id=? AND version=?""",
            ((body.user_alias or "").strip() or None, utcnow(), account_id, body.version),
        )
        if not changed:
            raise _error(409, "version_conflict", "账号信息已经变化，请刷新后重试")
        return await database.fetch_one("SELECT * FROM accounts WHERE account_id=?", (account_id,))

    @router.get("/sources")
    async def get_sources(
        account_id: str | None = None,
        source_type: str | None = Query(default=None, alias="type"),
        q: str = "",
        _session=Depends(require_session),
    ):
        clauses, params = [], []
        if account_id:
            clauses.append("s.account_id=?")
            params.append(account_id)
        if source_type:
            if source_type not in {"group", "contact"}:
                raise _error(400, "invalid_source_type", "来源类型无效")
            clauses.append("s.source_type=?")
            params.append(source_type)
        if q:
            clauses.append("(s.peer_id LIKE ? OR COALESCE(s.user_alias, s.platform_name, '') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await database.fetch_all(
            """SELECT s.*, COALESCE(s.user_alias, s.platform_name, s.peer_id) AS display_name,
               COALESCE(a.user_alias, a.platform_name, a.account_id) AS account_name, a.connected
               FROM sources s JOIN accounts a ON a.account_id=s.account_id""" + where + " ORDER BY account_name, display_name",
            params,
        )
        return {"items": rows}

    @router.post("/sources/refresh")
    async def refresh(account_id: str | None = Body(default=None, embed=True), _session=Depends(require_session)):
        return await refresh_sources(account_id)

    @router.post("/sources/manual", status_code=201)
    async def add_manual_source(body: ManualSource, _session=Depends(require_session)):
        if not body.account_id.strip() or not body.group_id.strip():
            raise _error(400, "invalid_source", "账号和群号不能为空")
        try:
            return await validate_source(body.account_id.strip(), body.group_id.strip())
        except ValueError as exc:
            raise _error(400, "membership_required", str(exc)) from exc

    @router.patch("/sources/{source_id}")
    async def patch_source(source_id: int, body: SourcePatch, _session=Depends(require_session)):
        row = await database.fetch_one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not row:
            raise _error(404, "source_not_found", "来源不存在")
        if body.enabled and row["error_code"] in {"not_in_group", "not_in_contacts"}:
            raise _error(400, "membership_required", "该来源已不在账号列表中，刷新确认后才能启用")
        enabled = int(body.enabled) if body.enabled is not None else row["enabled"]
        alias = (body.user_alias or "").strip() or None if body.user_alias is not None else row["user_alias"]
        changed = await database.execute(
            """UPDATE sources SET enabled=?, user_alias=?, error_code=CASE WHEN ? THEN NULL ELSE error_code END,
               updated_at=?, version=version+1 WHERE id=? AND version=?""",
            (enabled, alias, enabled, utcnow(), source_id, body.version),
        )
        if not changed:
            raise _error(409, "version_conflict", "来源信息已经变化，请刷新后重试")
        await database.execute(
            "INSERT INTO audit(occurred_at, object_type, object_id, action) VALUES (?, 'source', ?, ?)",
            (utcnow(), str(source_id), "enable" if enabled else "disable"),
        )
        return await database.fetch_one("SELECT * FROM sources WHERE id=?", (source_id,))

    @router.get("/templates")
    async def get_templates(include_archived: bool = False, _session=Depends(require_session)):
        where = "" if include_archived else " WHERE t.status='active'"
        rows = await database.fetch_all(
            """SELECT t.*, tr.fields_json FROM templates t
               JOIN template_revisions tr ON tr.template_id=t.id AND tr.version=t.current_version"""
            + where + " ORDER BY t.built_in DESC, t.name"
        )
        for row in rows:
            row["fields"] = _json(row.pop("fields_json"), [])
        return {"items": rows}

    @router.post("/templates", status_code=201)
    async def create_template(body: TemplateCreate, _session=Depends(require_session)):
        try:
            fields = validate_template_fields(body.fields)
        except ValueError as exc:
            raise _error(400, "invalid_template", str(exc)) from exc
        template_id, now = str(uuid.uuid4()), utcnow()
        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                "INSERT INTO templates(id,name,built_in,status,current_version,created_at,updated_at) VALUES (?,?,0,'active',1,?,?)",
                (template_id, body.name.strip(), now, now),
            )
            connection.execute(
                "INSERT INTO template_revisions(template_id,version,fields_json,created_at) VALUES (?,1,?,?)",
                (template_id, json.dumps(fields, ensure_ascii=False), now),
            )
            return dict(connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()) | {"fields": fields}
        return await database.write(write)

    @router.patch("/templates/{template_id}")
    async def patch_template(template_id: str, body: TemplatePatch, _session=Depends(require_session)):
        try:
            fields = validate_template_fields(body.fields)
        except ValueError as exc:
            raise _error(400, "invalid_template", str(exc)) from exc
        now = utcnow()
        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            template = connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
            if not template:
                raise KeyError
            if template["version"] != body.version:
                raise RuntimeError("version")
            next_version = template["current_version"] + 1
            connection.execute(
                "INSERT INTO template_revisions(template_id,version,fields_json,created_at) VALUES (?,?,?,?)",
                (template_id, next_version, json.dumps(fields, ensure_ascii=False), now),
            )
            connection.execute(
                "UPDATE templates SET name=?,current_version=?,updated_at=?,version=version+1 WHERE id=?",
                (body.name.strip(), next_version, now, template_id),
            )
            return dict(connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()) | {"fields": fields}
        try:
            return await database.write(write)
        except KeyError as exc:
            raise _error(404, "template_not_found", "业务模式不存在") from exc
        except RuntimeError as exc:
            raise _error(409, "version_conflict", "业务模式已经变化，请刷新后重试") from exc

    @router.post("/templates/{template_id}/copy", status_code=201)
    async def copy_template(template_id: str, _session=Depends(require_session)):
        template, fields = await template_revision(template_id)
        return await create_template(TemplateCreate(name=f"{template['name']} 副本", fields=fields), _session)

    @router.post("/templates/{template_id}/reset")
    async def reset_template(template_id: str, version: int = Body(embed=True), _session=Depends(require_session)):
        template = await database.fetch_one("SELECT * FROM templates WHERE id=?", (template_id,))
        if not template or not template["built_in"]:
            raise _error(400, "template_not_resettable", "只有内置业务模式可以重置")
        return await patch_template(template_id, TemplatePatch(name="电脑经销商模式", fields=DEFAULT_TEMPLATE_FIELDS, version=version), _session)

    @router.post("/templates/{template_id}/archive")
    async def archive_template(template_id: str, version: int = Body(embed=True), _session=Depends(require_session)):
        changed = await database.execute(
            "UPDATE templates SET status='archived',updated_at=?,version=version+1 WHERE id=? AND version=? AND built_in=0",
            (utcnow(), template_id, version),
        )
        if not changed:
            raise _error(409, "template_not_archived", "内置模式不能停用，或业务模式已经变化")
        return {"archived": True}

    @router.get("/tasks")
    async def get_tasks(
        limit: int = settings.page_size,
        cursor: str | None = None,
        include_archived: bool = False,
        _session=Depends(require_session),
    ):
        size = limit_value(limit)
        point = _decode_cursor(cursor)
        clauses = [] if include_archived else ["t.status!='archived'"]
        params: list[Any] = []
        if point:
            clauses.append("(t.updated_at<? OR (t.updated_at=? AND t.id<?))")
            params.extend([point[0], point[0], point[1]])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await database.fetch_all(
            """SELECT t.*,
               tm.name AS template_name, tm.current_version AS latest_template_version,
               tr.template_version, tr.conditions_json,
               (SELECT COUNT(*) FROM leads l WHERE l.task_id=t.id AND l.seen_at IS NULL AND l.superseded_at IS NULL) AS new_leads,
               (SELECT COUNT(*) FROM leads l WHERE l.task_id=t.id AND l.outcome='review' AND l.superseded_at IS NULL) AS review_leads,
               (SELECT MAX(processed_at) FROM evaluations e WHERE e.task_id=t.id) AS last_processed_at
               FROM tasks t JOIN templates tm ON tm.id=t.template_id
               JOIN task_revisions tr ON tr.task_id=t.id AND tr.revision=t.current_revision"""
               + where + " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?",
            (*params, size + 1),
        )
        more = len(rows) > size
        rows = rows[:size]
        for row in rows:
            conditions = _json(row.pop("conditions_json"), {})
            row["values"] = values_from_conditions(conditions)
        return {"items": rows, "next_cursor": _cursor(rows[-1]["updated_at"], rows[-1]["id"]) if more and rows else None}

    def check_enabled_sources(connection: sqlite3.Connection, source_ids: list[int]) -> None:
        placeholders = ",".join("?" for _ in source_ids)
        count = connection.execute(
            f"SELECT COUNT(*) FROM sources WHERE enabled=1 AND id IN ({placeholders})", source_ids
        ).fetchone()[0]
        if count != len(source_ids):
            raise ValueError("任务只能选择当前已启用的来源")

    @router.post("/tasks", status_code=201)
    async def create_task(body: TaskCreate, _session=Depends(require_session)):
        template, fields = await template_revision(body.template_id, body.template_version)
        selected_version = int(body.template_version or template["current_version"])
        if template["status"] != "active" or selected_version != template["current_version"]:
            raise _error(409, "template_version_conflict", "请使用业务模式的最新版本新建任务")
        try:
            conditions = validate_conditions(body.conditions, fields if "values" in body.conditions else None)
        except ValueError as exc:
            raise _error(400, "invalid_task", str(exc)) from exc
        task_id, now = str(uuid.uuid4()), utcnow()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            check_enabled_sources(connection, conditions["source_ids"])
            connection.execute(
                "INSERT INTO tasks(id, name, template_id, status, current_revision, expires_at, created_at, updated_at) VALUES (?, ?, ?, 'active', 1, ?, ?, ?)",
                (task_id, body.name.strip(), body.template_id, body.expires_at, now, now),
            )
            connection.execute(
                "INSERT INTO task_revisions(task_id, revision, template_version, conditions_json, created_at) VALUES (?, 1, ?, ?, ?)",
                (task_id, str(selected_version), json.dumps(conditions, ensure_ascii=False), now),
            )
            connection.executemany(
                "INSERT INTO task_revision_sources(task_id, revision, source_id) VALUES (?, 1, ?)",
                [(task_id, source_id) for source_id in conditions["source_ids"]],
            )
            connection.execute(
                "INSERT INTO audit(occurred_at, object_type, object_id, action) VALUES (?, 'task', ?, 'create')",
                (now, task_id),
            )
            return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

        try:
            return await database.write(write)
        except ValueError as exc:
            raise _error(400, "invalid_sources", str(exc)) from exc

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, _session=Depends(require_session)):
        task = await database.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise _error(404, "task_not_found", "任务不存在")
        revision = await database.fetch_one(
            "SELECT * FROM task_revisions WHERE task_id=? AND revision=?", (task_id, task["current_revision"])
        )
        task["conditions"] = _json(revision["conditions_json"] if revision else None, {})
        task["values"] = values_from_conditions(task["conditions"])
        template, fields = await template_revision(task["template_id"], int(revision["template_version"]) if revision else None)
        task["template_name"] = template["name"]
        task["template_fields"] = fields
        task["template_version"] = int(revision["template_version"]) if revision else template["current_version"]
        task["latest_template_version"] = template["current_version"]
        task["revisions"] = await database.fetch_all(
            "SELECT revision, template_version, created_at FROM task_revisions WHERE task_id=? ORDER BY revision DESC", (task_id,)
        )
        return task

    @router.patch("/tasks/{task_id}")
    async def patch_task(task_id: str, body: dict[str, Any], _session=Depends(require_session)):
        expected_version = body.get("version")
        if not isinstance(expected_version, int):
            raise _error(400, "version_required", "缺少任务版本号")
        now = utcnow()

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError
            if task["version"] != expected_version:
                raise RuntimeError("version")
            name = str(body.get("name", task["name"])).strip()
            status = body.get("status", task["status"])
            expires_at = body.get("expires_at", task["expires_at"])
            if not name or status not in {"active", "paused", "completed", "archived"}:
                raise ValueError("任务名称或状态无效")
            revision = task["current_revision"]
            if "conditions" in body:
                current_revision = connection.execute(
                    "SELECT template_version FROM task_revisions WHERE task_id=? AND revision=?",
                    (task_id, revision),
                ).fetchone()
                template = connection.execute("SELECT * FROM templates WHERE id=?", (task["template_id"],)).fetchone()
                selected_version = int(body.get("template_version") or current_revision["template_version"])
                if selected_version not in {int(current_revision["template_version"]), int(template["current_version"])}:
                    raise RuntimeError("template_version")
                template_row = connection.execute(
                    "SELECT fields_json FROM template_revisions WHERE template_id=? AND version=?",
                    (task["template_id"], selected_version),
                ).fetchone()
                if not template_row:
                    raise RuntimeError("template_version")
                fields = _json(template_row["fields_json"], [])
                conditions = validate_conditions(body["conditions"], fields if "values" in body["conditions"] else None)
                check_enabled_sources(connection, conditions["source_ids"])
                revision += 1
                connection.execute(
                    "INSERT INTO task_revisions(task_id, revision, template_version, conditions_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, revision, str(selected_version), json.dumps(conditions, ensure_ascii=False), now),
                )
                connection.executemany(
                    "INSERT INTO task_revision_sources(task_id, revision, source_id) VALUES (?, ?, ?)",
                    [(task_id, revision, source_id) for source_id in conditions["source_ids"]],
                )
            connection.execute(
                """UPDATE tasks SET name=?, status=?, expires_at=?, current_revision=?,
                   updated_at=?, version=version+1 WHERE id=?""",
                (name, status, expires_at, revision, now, task_id),
            )
            if status != "active":
                connection.execute(
                    "UPDATE processing_jobs SET status='cancelled', updated_at=? WHERE task_id=? AND status IN ('pending','deferred')",
                    (now, task_id),
                )
            connection.execute(
                "INSERT INTO audit(occurred_at, object_type, object_id, action) VALUES (?, 'task', ?, 'update')",
                (now, task_id),
            )
            return dict(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

        try:
            return await database.write(write)
        except KeyError as exc:
            raise _error(404, "task_not_found", "任务不存在") from exc
        except RuntimeError as exc:
            code = "template_version_conflict" if str(exc) == "template_version" else "version_conflict"
            message = "业务模式版本已变化，请刷新后重试" if code == "template_version_conflict" else "任务已经变化，请刷新后重试"
            raise _error(409, code, message) from exc
        except ValueError as exc:
            raise _error(400, "invalid_task", str(exc)) from exc

    @router.post("/tasks/{task_id}/delete-preview")
    async def delete_task_preview(task_id: str, delete_leads: bool = False, _session=Depends(require_session)):
        task = await database.fetch_one("SELECT id FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise _error(404, "task_not_found", "任务不存在")
        count = await database.fetch_one("SELECT COUNT(*) AS value FROM leads WHERE task_id=?", (task_id,))
        scope = f"delete-task:{task_id}:{int(delete_leads)}"
        return {"task_id": task_id, "lead_count": count["value"], "delete_leads": delete_leads, "confirmation_token": issue_confirmation(scope)}

    @router.delete("/tasks/{task_id}")
    async def delete_task(task_id: str, delete_leads: bool, confirmation_token: str, _session=Depends(require_session)):
        scope = f"delete-task:{task_id}:{int(delete_leads)}"
        if not consume_confirmation(confirmation_token, scope):
            raise _error(400, "confirmation_required", "删除确认已过期，请重新预览")
        if not delete_leads:
            await database.execute("UPDATE tasks SET status='archived', updated_at=?, version=version+1 WHERE id=?", (utcnow(), task_id))
            return {"archived": True}
        changed = await database.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if not changed:
            raise _error(404, "task_not_found", "任务不存在")
        return {"deleted": True}

    @router.get("/messages")
    async def get_messages(
        limit: int = settings.page_size,
        cursor: str | None = None,
        q: str = "",
        source_id: int | None = None,
        result: str | None = None,
        _session=Depends(require_session),
    ):
        size, point = limit_value(limit), _decode_cursor(cursor)
        clauses, params = [], []
        if q:
            clauses.append("r.text LIKE ?")
            params.append(f"%{q}%")
        if source_id:
            clauses.append("s.id=?")
            params.append(source_id)
        if result == "failed":
            clauses.append("EXISTS(SELECT 1 FROM processing_jobs jf WHERE jf.message_id=r.id AND jf.status='failed')")
        elif result == "pending":
            clauses.append("e.outcome IS NULL AND NOT EXISTS(SELECT 1 FROM processing_jobs jf WHERE jf.message_id=r.id AND jf.status='failed')")
        elif result:
            clauses.append("e.outcome=?")
            params.append(result)
        if point:
            clauses.append("(r.received_at<? OR (r.received_at=? AND r.id<?))")
            params.extend([point[0], point[0], point[1]])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await database.fetch_all(
            """SELECT r.id, r.account_id, r.source_type, r.peer_id, r.sender_id, r.sender_name, r.message_time,
               r.received_at, r.text, r.expires_at,
               COALESCE(s.user_alias, s.platform_name, s.peer_id) AS source_name,
               COALESCE(e.outcome, CASE WHEN EXISTS(SELECT 1 FROM processing_jobs j WHERE j.message_id=r.id AND j.status='failed') THEN 'failed' ELSE 'pending' END) AS result
               FROM raw_messages r
               LEFT JOIN sources s ON s.id=r.source_id
               LEFT JOIN evaluations e ON e.id=(SELECT e2.id FROM evaluations e2 WHERE e2.message_id=r.id ORDER BY e2.processed_at DESC LIMIT 1)"""
            + where + " ORDER BY r.received_at DESC, r.id DESC LIMIT ?",
            (*params, size + 1),
        )
        more, rows = len(rows) > size, rows[:size]
        return {"items": rows, "next_cursor": _cursor(rows[-1]["received_at"], rows[-1]["id"]) if more and rows else None}

    @router.get("/messages/{message_id}")
    async def get_message(message_id: str, _session=Depends(require_session)):
        row = await database.fetch_one("SELECT * FROM raw_messages WHERE id=?", (message_id,))
        if not row:
            raise _error(404, "message_expired_or_missing", "原文已过期或不存在")
        row["segments"] = _json(row.pop("segments_json"), [])
        row["evaluations"] = await database.fetch_all("SELECT * FROM evaluations WHERE message_id=? ORDER BY processed_at DESC", (message_id,))
        row["leads"] = await database.fetch_all("SELECT * FROM leads WHERE message_id=? ORDER BY created_at DESC", (message_id,))
        return row

    @router.post("/tasks/{task_id}/replay-preview")
    async def replay_preview(task_id: str, body: ReplayBody, _session=Depends(require_session)):
        task = await database.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise _error(404, "task_not_found", "任务不存在")
        start_at = body.start_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_at = body.end_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        count = await database.fetch_one(
            """SELECT COUNT(*) AS value FROM raw_messages r
               JOIN task_revision_sources trs ON trs.source_id=r.source_id AND trs.task_id=? AND trs.revision=?
               WHERE r.received_at>=? AND r.received_at<=?""",
            (task_id, task["current_revision"], start_at, end_at),
        )
        return {"eligible_count": count["value"], "start_at": start_at, "end_at": end_at, "limitation": "只处理仍在原文保留期内的数据"}

    @router.post("/tasks/{task_id}/replays", status_code=202)
    async def create_replay(task_id: str, body: ReplayBody, _session=Depends(require_session)):
        replay_id, now = str(uuid.uuid4()), utcnow()
        start_at = body.start_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_at = body.end_at.astimezone(timezone.utc).isoformat(timespec="seconds")

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError
            messages = connection.execute(
                """SELECT r.id FROM raw_messages r
                   JOIN task_revision_sources trs ON trs.source_id=r.source_id AND trs.task_id=? AND trs.revision=?
                   WHERE r.received_at>=? AND r.received_at<=?""",
                (task_id, task["current_revision"], start_at, end_at),
            ).fetchall()
            replay_status = "running" if messages else "completed"
            connection.execute(
                """INSERT INTO replays(
                     id, task_id, revision, start_at, end_at, status, eligible_count, created_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (replay_id, task_id, task["current_revision"], start_at, end_at, replay_status, len(messages), now, now if not messages else None),
            )
            for message in messages:
                connection.execute(
                    """INSERT INTO processing_jobs(id, message_id, task_id, revision, processor_version, status, next_attempt_at, replay_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                       ON CONFLICT(message_id, task_id, revision, processor_version) DO UPDATE SET
                         status='pending', attempts=0, next_attempt_at=excluded.next_attempt_at,
                         lease_until=NULL, error_code=NULL, replay_id=excluded.replay_id, updated_at=excluded.updated_at""",
                    (str(uuid.uuid4()), message["id"], task_id, task["current_revision"], PROCESSOR_VERSION, now, replay_id, now, now),
                )
            return {"id": replay_id, "eligible_count": len(messages), "status": replay_status}

        try:
            return await database.write(write)
        except KeyError as exc:
            raise _error(404, "task_not_found", "任务不存在") from exc

    @router.get("/leads")
    async def get_leads(
        limit: int = settings.page_size,
        cursor: str | None = None,
        task_id: str | None = None,
        direction: str | None = None,
        outcome: str | None = None,
        manual_status: str | None = None,
        q: str = "",
        include_conflicts: bool = False,
        _session=Depends(require_session),
    ):
        size, point = limit_value(limit), _decode_cursor(cursor)
        clauses, params = ["l.superseded_at IS NULL"], []
        if not include_conflicts and not outcome:
            clauses.append("l.outcome!='conflict'")
        for field, value in (("l.task_id", task_id), ("l.direction", direction), ("l.outcome", outcome), ("l.manual_status", manual_status)):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        if q:
            clauses.append("(r.text LIKE ? OR r.sender_name LIKE ? OR t.name LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        if point:
            clauses.append("(l.created_at<? OR (l.created_at=? AND l.id<?))")
            params.extend([point[0], point[0], point[1]])
        rows = await database.fetch_all(
            """SELECT l.*, t.name AS task_name, r.sender_id, r.sender_name, r.message_time,
               COALESCE(s.user_alias, s.platform_name, s.peer_id) AS source_name,
               s.source_type, s.peer_id
               FROM leads l JOIN tasks t ON t.id=l.task_id
               LEFT JOIN raw_messages r ON r.id=l.message_id
               LEFT JOIN sources s ON s.id=r.source_id
               WHERE """ + " AND ".join(clauses) + " ORDER BY l.created_at DESC, l.id DESC LIMIT ?",
            (*params, size + 1),
        )
        more, rows = len(rows) > size, rows[:size]
        for row in rows:
            row["extracted"] = _json(row.pop("extracted_json"), {})
            row["evidence"] = _json(row.pop("evidence_json"), {})
            row["comparisons"] = _json(row.pop("comparison_json"), [])
            row["manual_overrides"] = _json(row.pop("manual_overrides_json"), {})
        return {"items": rows, "next_cursor": _cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None}

    @router.get("/leads/{lead_id}")
    async def get_lead(lead_id: str, _session=Depends(require_session)):
        row = await database.fetch_one(
            """SELECT l.*, t.name AS task_name, r.text AS raw_text, r.sender_id, r.sender_name,
               r.message_time, r.account_id, r.source_type, r.peer_id
               FROM leads l JOIN tasks t ON t.id=l.task_id LEFT JOIN raw_messages r ON r.id=l.message_id WHERE l.id=?""",
            (lead_id,),
        )
        if not row:
            raise _error(404, "lead_not_found", "线索不存在")
        for source, target, default in (
            ("extracted_json", "extracted", {}), ("evidence_json", "evidence", {}),
            ("comparison_json", "comparisons", []), ("manual_overrides_json", "manual_overrides", {}),
        ):
            row[target] = _json(row.pop(source), default)
        return row

    @router.patch("/leads/{lead_id}")
    async def patch_lead(lead_id: str, body: LeadPatch, _session=Depends(require_session)):
        row = await database.fetch_one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not row:
            raise _error(404, "lead_not_found", "线索不存在")
        status = body.manual_status or row["manual_status"]
        if status not in {"new", "contacted", "ignored", "completed"}:
            raise _error(400, "invalid_status", "人工状态无效")
        changed = await database.execute(
            """UPDATE leads SET manual_status=?, note=?, manual_overrides_json=?,
               seen_at=?, updated_at=?, version=version+1 WHERE id=? AND version=?""",
            (
                status,
                body.note if body.note is not None else row["note"],
                json.dumps(body.manual_overrides, ensure_ascii=False) if body.manual_overrides is not None else row["manual_overrides_json"],
                utcnow() if body.seen else (None if body.seen is False else row["seen_at"]),
                utcnow(), lead_id, body.version,
            ),
        )
        if not changed:
            raise _error(409, "version_conflict", "线索已经变化，请刷新后重试")
        return await get_lead(lead_id, _session)

    @router.post("/leads/mark-read")
    async def mark_read(task_id: str | None = None, _session=Depends(require_session)):
        sql, params = "UPDATE leads SET seen_at=?, updated_at=?, version=version+1 WHERE seen_at IS NULL", [utcnow(), utcnow()]
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        return {"updated": await database.execute(sql, params)}

    @router.post("/tasks/{task_id}/manual-leads", status_code=201)
    async def manual_lead(task_id: str, message_id: str = Body(embed=True), _session=Depends(require_session)):
        now = utcnow()
        message = await database.fetch_one("SELECT * FROM raw_messages WHERE id=?", (message_id,))
        task = await database.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not message:
            raise _error(404, "message_expired_or_missing", "原文已过期或不存在")
        if not task:
            raise _error(404, "task_not_found", "任务不存在")
        key = "manual-" + hashlib.sha256(message_id.encode()).hexdigest()[:16]
        lead_days = int(await database.get_setting("lead_days") or settings.lead_days)
        lead_id = str(uuid.uuid4())
        await database.execute(
            """INSERT INTO leads(id, task_id, message_id, item_key, revision, processor_version, direction,
               extracted_json, evidence_json, comparison_json, outcome, reason, evidence_expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'unknown', '{}', ?, '[]', 'review', '人工加入', ?, ?, ?)
               ON CONFLICT(task_id, message_id, item_key) DO UPDATE SET superseded_at=NULL, updated_at=excluded.updated_at""",
            (lead_id, task_id, message_id, key, task["current_revision"], PROCESSOR_VERSION,
             json.dumps({"text": message["text"], "manual": True}, ensure_ascii=False),
             (datetime.now(timezone.utc) + timedelta(days=lead_days)).isoformat(timespec="seconds"), now, now),
        )
        row = await database.fetch_one("SELECT id FROM leads WHERE task_id=? AND message_id=? AND item_key=?", (task_id, message_id, key))
        return {"id": row["id"], "manual": True}

    @router.get("/settings/retention")
    async def get_retention(_session=Depends(require_session)):
        return (await service.status())["retention"]

    @router.post("/settings/retention-preview")
    async def retention_preview(body: RetentionPatch, _session=Depends(require_session)):
        current = (await service.status())["retention"]
        destructive = body.raw_days < current["raw_days"] or body.lead_days < current["lead_days"] or body.diagnostic_days < current["diagnostic_days"] or body.backup_keep < current["backup_keep"]
        counts = {"messages": 0, "leads": 0, "diagnostics": 0, "backups": 0}
        if destructive:
            raw_cutoff = (datetime.now(timezone.utc) - timedelta(days=body.raw_days)).isoformat(timespec="seconds")
            lead_cutoff = (datetime.now(timezone.utc) - timedelta(days=body.lead_days)).isoformat(timespec="seconds")
            diagnostic_cutoff = (datetime.now(timezone.utc) - timedelta(days=body.diagnostic_days)).isoformat(timespec="seconds")
            for key, sql, value in (
                ("messages", "SELECT COUNT(*) AS value FROM raw_messages WHERE received_at<?", raw_cutoff),
                ("leads", "SELECT COUNT(*) AS value FROM leads WHERE created_at<?", lead_cutoff),
                ("diagnostics", "SELECT COUNT(*) AS value FROM diagnostics WHERE occurred_at<?", diagnostic_cutoff),
            ):
                row = await database.fetch_one(sql, (value,))
                counts[key] = row["value"]
            counts["backups"] = max(0, len(list(settings.backup_dir.glob("huginn-leads-*.db"))) - body.backup_keep)
        return {"destructive": destructive, "estimated_deletions": counts, "confirmation_token": issue_confirmation("retention") if destructive else None}

    @router.patch("/settings/retention")
    async def patch_retention(body: RetentionPatch, _session=Depends(require_session)):
        current = (await service.status())["retention"]
        destructive = body.raw_days < current["raw_days"] or body.lead_days < current["lead_days"] or body.diagnostic_days < current["diagnostic_days"] or body.backup_keep < current["backup_keep"]
        if destructive and not consume_confirmation(body.confirmation_token, "retention"):
            raise _error(400, "confirmation_required", "缩短保留期前必须重新预览并确认")
        values = {
            key: int(getattr(body, key))
            for key in ("raw_days", "lead_days", "diagnostic_days", "backup_keep")
        }
        await service.update_retention(values)
        if destructive:
            await service.cleanup()
            await service._trim_backups()
        return (await service.status())["retention"]

    @router.get("/backups")
    async def get_backups(_session=Depends(require_session)):
        items = [
            {"name": path.name, "size": path.stat().st_size, "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for path in sorted(settings.backup_dir.glob("huginn-leads-*.db"), reverse=True)
        ]
        return {"items": items, "restore_command": "restore.cmd backups\\<备份文件名>"}

    @router.post("/backups", status_code=201)
    async def create_backup(_session=Depends(require_session)):
        path = await service.backup()
        return {"name": path.name, "size": path.stat().st_size}

    @router.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str, _session=Depends(require_session)):
        now = utcnow()

        def write(connection: sqlite3.Connection) -> bool:
            job = connection.execute(
                "SELECT replay_id FROM processing_jobs WHERE id=? AND status='failed'", (job_id,)
            ).fetchone()
            if not job:
                return False
            connection.execute(
                "UPDATE processing_jobs SET status='pending', attempts=0, error_code=NULL, next_attempt_at=?, updated_at=? WHERE id=?",
                (now, now, job_id),
            )
            if job["replay_id"]:
                connection.execute(
                    "UPDATE replays SET status='running', completed_at=NULL WHERE id=?", (job["replay_id"],)
                )
            return True

        changed = await database.write(write)
        if not changed:
            raise _error(404, "failed_job_not_found", "没有找到可重试的失败任务")
        return {"queued": True}

    @router.get("/jobs")
    async def get_jobs(status: str = "failed", limit: int = 50, _session=Depends(require_session)):
        if status not in {"failed", "pending", "deferred", "processing", "source_expired", "cancelled"}:
            raise _error(400, "invalid_status", "处理作业状态无效")
        rows = await database.fetch_all(
            """SELECT j.id, j.status, j.attempts, j.error_code, j.updated_at, j.task_id,
               t.name AS task_name, r.id AS message_id, r.received_at
               FROM processing_jobs j JOIN tasks t ON t.id=j.task_id
               LEFT JOIN raw_messages r ON r.id=j.message_id
               WHERE j.status=? ORDER BY j.updated_at DESC LIMIT ?""",
            (status, limit_value(limit)),
        )
        return {"items": rows}

    @router.post("/data/delete-preview")
    async def delete_preview(
        source_id: int | None = None,
        before: str | None = None,
        _session=Depends(require_session),
    ):
        clauses, params = [], []
        if source_id:
            clauses.append("r.source_id=?")
            params.append(source_id)
        if before:
            clauses.append("r.received_at<?")
            params.append(before)
        if not clauses:
            raise _error(400, "delete_scope_required", "必须指定来源或截止时间")
        where = " AND ".join(clauses)
        row = await database.fetch_one(f"SELECT COUNT(*) AS value FROM raw_messages r WHERE {where}", params)
        impacted = await database.fetch_all(
            f"SELECT DISTINCT l.task_id, t.name FROM leads l JOIN tasks t ON t.id=l.task_id JOIN raw_messages r ON r.id=l.message_id WHERE {where}", params
        )
        scope = json.dumps({"source_id": source_id, "before": before}, sort_keys=True)
        return {"message_count": row["value"], "affected_tasks": impacted, "confirmation_token": issue_confirmation("delete:" + scope)}

    @router.post("/data/delete")
    async def delete_data(
        source_id: int | None = None,
        before: str | None = None,
        confirmation_token: str = Body(embed=True),
        _session=Depends(require_session),
    ):
        scope = json.dumps({"source_id": source_id, "before": before}, sort_keys=True)
        if not consume_confirmation(confirmation_token, "delete:" + scope):
            raise _error(400, "confirmation_required", "删除确认已过期，请重新预览")
        clauses, params = [], []
        if source_id:
            clauses.append("r.source_id=?")
            params.append(source_id)
        if before:
            clauses.append("r.received_at<?")
            params.append(before)
        where = " AND ".join(clauses)

        def write(connection: sqlite3.Connection) -> int:
            connection.execute(f"DELETE FROM leads WHERE message_id IN (SELECT r.id FROM raw_messages r WHERE {where})", params)
            return connection.execute(f"DELETE FROM raw_messages WHERE {where}", params).rowcount

        return {"deleted_messages": await database.write(write)}

    app.include_router(router)
