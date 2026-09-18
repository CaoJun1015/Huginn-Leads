from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import nonebot
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nonebot import get_bots, get_driver, on_message
from nonebot.log import logger
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent

from .api import install_api
from .config import load_settings
from .database import Database
from .security import Security
from .service import Workbench
from .instance import InstanceLock

_installed = False


def _safe_segments(event: MessageEvent) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for segment in event.original_message:
        data: dict[str, Any] = {}
        for key, value in segment.data.items():
            text = str(value)
            if key == "file" and (text.startswith("base64://") or len(text) > 2048):
                continue
            data[str(key)] = text[:2048]
        segments.append({"type": str(segment.type)[:40], "data": data})
    return segments


def _event_size(event: MessageEvent) -> int:
    try:
        payload = event.model_dump(mode="json")
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return len(str(event.original_message).encode("utf-8"))


def _redact_framework_log(record: dict[str, Any]) -> None:
    # NoneBot logs events before receive(), including ignored sources. Adapter
    # parse errors and handler tracebacks can also contain the entire payload.
    if (record["name"] or "").startswith(("nonebot.message", "nonebot.internal.matcher", "nonebot.adapters.onebot", "nonebot.utils")):
        exception = record["exception"]
        kind = exception.type.__name__ if exception else "none"
        record["message"] = f"framework_event code={record['function']} kind={kind}"
        record["exception"] = None


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    logger.configure(patcher=_redact_framework_log)

    settings = load_settings()
    database = Database(settings.database_path, Path(__file__).with_name("migrations"))
    instance_lock = InstanceLock(settings.database_path)
    security = Security(database)
    service = Workbench(database, settings)
    driver = get_driver()
    app = nonebot.get_app()

    async def refresh(account_id: str | None) -> dict[str, Any]:
        bots = get_bots()
        selected = [account_id] if account_id else sorted(bots)
        results, errors = [], []
        for identifier in selected:
            bot = bots.get(identifier)
            if not bot:
                errors.append({"account_id": identifier, "code": "account_not_connected"})
                continue
            result = {"account_id": identifier, "groups": 0, "contacts": 0}
            for source_type, api_name, result_key in (
                ("group", "get_group_list", "groups"), ("contact", "get_friend_list", "contacts")
            ):
                try:
                    items = await bot.call_api(api_name)
                    result[result_key] = await service.refresh_sources(identifier, list(items or []), source_type)
                except Exception as exc:
                    code = f"{source_type}_refresh_failed"
                    await service.record_diagnostic(code, identifier, details={"kind": type(exc).__name__})
                    errors.append({"account_id": identifier, "type": source_type, "code": code})
            results.append(result)
        return {"results": results, "errors": errors}

    async def probe_connections() -> dict[str, Any]:
        bots = get_bots()
        accounts = []
        for identifier, bot in sorted(bots.items()):
            try:
                info = await asyncio.wait_for(bot.call_api("get_login_info"), timeout=3)
                accounts.append({
                    "account_id": str(identifier), "nickname": str((info or {}).get("nickname") or ""),
                    "state": "ok", "message": "QQ 已登录",
                })
            except Exception:
                accounts.append({"account_id": str(identifier), "state": "error", "message": "无法读取 QQ 登录信息"})
        return {
            "napcatqq": {
                "state": "ok" if bots else "warning",
                "message": f"{len(bots)} 个反向 WebSocket 已连接" if bots else "尚未检测到 NapCatQQ 连接",
            },
            "qq": {
                "state": "ok" if accounts and all(item["state"] == "ok" for item in accounts) else ("error" if accounts else "warning"),
                "message": f"{sum(item['state'] == 'ok' for item in accounts)} 个 QQ 已登录" if accounts else "尚无 QQ 登录",
                "accounts": accounts,
            },
        }

    async def validate_source(account_id: str, group_id: str) -> dict[str, Any]:
        bot = get_bots().get(account_id)
        if not bot:
            raise ValueError("对应 QQ 账号当前未连接")
        try:
            info = await bot.call_api("get_group_info", group_id=int(group_id), no_cache=True)
        except Exception as exc:
            await service.record_diagnostic(
                "source_membership_failed", account_id, details={"kind": type(exc).__name__}
            )
            raise ValueError("无法确认该账号属于此群") from exc
        return await service.upsert_source(account_id, dict(info or {}))

    install_api(app, database, service, security, settings, refresh, validate_source, probe_connections)

    @driver.on_startup
    async def startup() -> None:
        instance_lock.acquire()
        try:
            await database.migrate()
            await security.start()
            await service.start()
        except BaseException:
            instance_lock.release()
            raise

    @driver.on_shutdown
    async def shutdown() -> None:
        try:
            await service.stop()
        finally:
            instance_lock.release()

    @driver.on_bot_connect
    async def connected(bot: Bot) -> None:
        name = None
        try:
            info = await bot.call_api("get_login_info")
            name = str(info.get("nickname") or "").strip() or None
        except Exception:
            pass
        await service.connect_account(str(bot.self_id), name)
        print(f"[Huginn Leads] QQ {bot.self_id} 已连接", flush=True)

    @driver.on_bot_disconnect
    async def disconnected(bot: Bot) -> None:
        await service.disconnect_account(str(bot.self_id))
        print(f"[Huginn Leads] QQ {bot.self_id} 已断开", flush=True)

    matcher = on_message(priority=1, block=False)

    @matcher.handle()
    async def receive(bot: Bot, event: Event) -> None:
        if isinstance(event, GroupMessageEvent):
            source_type, peer_id = "group", str(event.group_id)
        elif isinstance(event, PrivateMessageEvent) and str(event.sub_type) == "friend":
            source_type, peer_id = "contact", str(event.user_id)
        else:
            return
        account_id = str(bot.self_id)
        await service.touch_account(account_id)
        if _event_size(event) > settings.max_event_bytes:
            await service.record_diagnostic("event_too_large", account_id, details={"limit": settings.max_event_bytes})
            return
        sender_name = (getattr(event.sender, "card", None) or event.sender.nickname or "").strip() or str(event.user_id)
        payload = {
            "platform": "qq",
            "account_id": account_id,
            "source_type": source_type,
            "peer_id": peer_id,
            "native_message_id": str(event.message_id) if event.message_id is not None else "",
            "sender_id": str(event.user_id),
            "sender_name": sender_name,
            "message_time": datetime.fromtimestamp(event.time, timezone.utc).isoformat(timespec="seconds"),
            "text": event.get_plaintext(),
            "segments": _safe_segments(event),
        }
        result = await service.ingest(payload)
        if result.get("saved") and not result.get("duplicate"):
            print(
                f"[Huginn Leads] message_saved account={account_id} type={source_type} "
                f"peer={peer_id} message={payload['native_message_id']} jobs={result['jobs']}",
                flush=True,
            )

    static_dir = settings.static_dir
    assets = static_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="huginn-assets")

    @app.get("/", include_in_schema=False)
    async def index():
        file = static_dir / "index.html"
        return FileResponse(file) if file.exists() else JSONResponse({"message": "Frontend is not built. Run frontend\\npm run build."}, status_code=503)

    @app.get("/huginn-leads-logo.png", include_in_schema=False)
    async def logo():
        file = static_dir / "huginn-leads-logo.png"
        return FileResponse(file) if file.exists() else JSONResponse({"message": "Logo is not built."}, status_code=404)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "onebot/")):
            return JSONResponse({"code": "not_found", "message": "接口不存在"}, status_code=404)
        file = static_dir / "index.html"
        return FileResponse(file) if file.exists() else JSONResponse({"message": "Frontend is not built."}, status_code=503)
