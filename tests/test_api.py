import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from huginn_leads.api import install_api
from huginn_leads.config import Settings
from huginn_leads.database import Database
from huginn_leads.security import Security
from huginn_leads.service import Workbench


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            root=root, data_dir=root / "data", backup_dir=root / "backups",
            database_path=root / "data" / "api.db", static_dir=root / "static",
            origin="http://127.0.0.1:18080",
        )
        self.settings.data_dir.mkdir(); self.settings.backup_dir.mkdir()
        migrations = Path(__file__).resolve().parents[1] / "src" / "huginn_leads" / "migrations"
        self.db = Database(self.settings.database_path, migrations)
        asyncio.run(self.db.migrate())
        for key, value in {"raw_days": "7", "lead_days": "30", "diagnostic_days": "7", "backup_keep": "7"}.items():
            asyncio.run(self.db.set_setting(key, value))
        self.security = Security(self.db); self.security.setup_code = "test-setup-code"
        self.service = Workbench(self.db, self.settings)
        app = FastAPI()

        async def refresh(_account):
            return {"results": [], "errors": []}

        async def validate(account_id, group_id):
            return await self.service.upsert_source(
                account_id, {"group_id": group_id, "group_name": "手动测试群"}
            )

        self.probe_error = False
        async def probe():
            if self.probe_error:
                raise RuntimeError("probe failed")
            return {
                "napcatqq": {"state": "ok", "message": "1 个反向 WebSocket 已连接"},
                "qq": {"state": "ok", "message": "1 个 QQ 已登录", "accounts": [{"account_id": "10001", "state": "ok"}]},
            }

        install_api(app, self.db, self.service, self.security, self.settings, refresh, validate, probe)
        self.client = TestClient(app, base_url=self.settings.origin)
        self.origin = {"Origin": self.settings.origin}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def authenticate(self):
        response = self.client.post(
            "/api/auth/setup",
            headers=self.origin,
            json={"code": "test-setup-code", "password": "a-secure-local-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json()["csrf_token"]
        self.write_headers = {**self.origin, "X-CSRF-Token": self.csrf}

    def test_auth_and_error_contract(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")
        rejected = self.client.post("/api/auth/login", json={"password": "anything-long-enough"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["code"], "origin_rejected")
        self.authenticate()
        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["database_ok"])
        self.assertEqual(set(status.json()["components"]), {"huginn_leads", "nonebot2", "napcatqq", "qq"})
        self.assertEqual(status.json()["components"]["napcatqq"]["state"], "ok")
        self.probe_error = True
        failed_probe = self.client.get("/api/status").json()["components"]
        self.assertEqual((failed_probe["napcatqq"]["state"], failed_probe["qq"]["state"]), ("error", "error"))

    def test_jev_auth_validation_and_disabled_state(self):
        endpoint = "/api/ai/jev/classify"
        with patch("huginn_leads.api.classify_message") as classify:
            self.assertEqual(self.client.get("/api/ai/jev/status").status_code, 401)
            self.assertEqual(self.client.post(endpoint, json={"text": "求购电脑"}).status_code, 401)
            self.authenticate()
            status = self.client.get("/api/ai/jev/status").json()
            self.assertEqual(status, {"configured": False, "model": "jev-1.13.0"})
            self.assertEqual(self.client.post(endpoint, json={"text": "求购电脑"}).status_code, 403)
            self.assertEqual(self.client.post(endpoint, headers=self.origin, json={"text": "求购电脑"}).status_code, 403)
            response = self.client.post(endpoint, headers=self.write_headers, json={"text": " "})
            self.assertEqual(response.status_code, 422)
            classify.assert_not_called()
        response = self.client.post(endpoint, headers=self.write_headers, json={"text": "求购电脑"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "jev_not_configured")

    def test_jev_route_result_and_error_contract(self):
        from huginn_leads.jev import Classification, JevError, TokenUsage
        self.authenticate()
        result = Classification(model="jev-1.13.0", category="purchase",
                                probabilities={"purchase": 0.95, "other": 0.05},
                                confidence=0.9, needs_review=False,
                                usage=TokenUsage(input_tokens=100, output_tokens=0))
        with patch("huginn_leads.api.classify_message", return_value=result) as classify:
            response = self.client.post("/api/ai/jev/classify", headers=self.write_headers, json={
                "text": "求购电脑", "categories": {"purchase": "求购", "other": "其他"},
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), result.model_dump())
            self.assertEqual(classify.call_args.args[1].text, "求购电脑")
        with patch("huginn_leads.api.classify_message", side_effect=JevError(504, "jev_timeout", "请求超时")):
            response = self.client.post("/api/ai/jev/classify", headers=self.write_headers, json={"text": "求购电脑"})
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json()["code"], "jev_timeout")

    def test_source_and_task_flow(self):
        self.authenticate()
        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        source = self.client.get("/api/sources").json()["items"][0]
        enabled = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": source["version"]},
        )
        self.assertEqual(enabled.status_code, 200)
        conditions = {
            "direction": "purchase", "product": "办公本", "aliases": ["ThinkBook"],
            "alias_mode": "any", "specs": {}, "price": {}, "quantity": {}, "trade": {},
            "source_ids": [source["id"]],
        }
        created = self.client.post(
            "/api/tasks", headers=self.write_headers,
            json={"name": "采购办公本", "conditions": conditions},
        )
        self.assertEqual(created.status_code, 201, created.text)
        task = self.client.get(f"/api/tasks/{created.json()['id']}")
        self.assertEqual(task.status_code, 200)
        self.assertEqual(task.json()["conditions"]["aliases"], ["ThinkBook"])
        stale = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": False, "version": source["version"]},
        )
        self.assertEqual(stale.status_code, 409)

    def test_task_expiry_timezone_validation_and_region_contract(self):
        self.authenticate()
        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        source = self.client.get("/api/sources").json()["items"][0]
        source = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": source["version"]},
        ).json()
        conditions = {
            "direction": "purchase", "product": "办公本", "aliases": ["ThinkBook"],
            "alias_mode": "any", "specs": {}, "price": {}, "quantity": {},
            "trade": {"region": "上海"}, "source_ids": [source["id"]],
        }
        created = self.client.post(
            "/api/tasks", headers=self.write_headers,
            json={"name": "跨时区任务", "expires_at": "2099-07-01T00:00:00+08:00", "conditions": conditions},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["expires_at"], "2099-06-30T16:00:00+00:00")

        legacy = self.client.post(
            "/api/tasks", headers=self.write_headers,
            json={"name": "日期兼容任务", "expires_at": "2099-06-30", "conditions": conditions},
        )
        self.assertEqual(legacy.status_code, 201, legacy.text)
        local_cutoff = datetime.fromisoformat(legacy.json()["expires_at"]).astimezone()
        self.assertEqual(local_cutoff.strftime("%Y-%m-%d %H:%M"), "2099-07-01 00:00")

        region = next(field for field in self.client.get("/api/templates").json()["items"][0]["fields"] if field.get("role") == "region")
        self.assertFalse(region["match"])
        self.assertIn("仅记录，不参与匹配", region["help"])
        invalid = self.client.patch(
            f"/api/tasks/{created.json()['id']}", headers=self.write_headers,
            json={"version": created.json()["version"], "expires_at": "2099-06-30T00:00:00"},
        )
        self.assertEqual((invalid.status_code, invalid.json()["code"]), (400, "invalid_task"))

    def test_message_summary_and_sender_qq(self):
        self.authenticate()
        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        source = self.client.get("/api/sources").json()["items"][0]
        source = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": source["version"]},
        ).json()

        for name, alias in (("目标任务", "ThinkBook"), ("无关任务", "Dell")):
            response = self.client.post(
                "/api/tasks", headers=self.write_headers,
                json={"name": name, "conditions": {
                    "direction": "sell", "product": name, "aliases": [alias], "alias_mode": "any",
                    "specs": {}, "price": {}, "quantity": {}, "trade": {}, "source_ids": [source["id"]],
                }},
            )
            self.assertEqual(response.status_code, 201, response.text)

        saved = asyncio.run(self.service.ingest({
            "platform": "qq", "account_id": "10001", "group_id": "20001", "native_message_id": "summary-1",
            "sender_id": "30001", "sender_name": "测试发布者", "message_time": "2026-09-15T12:00:00+00:00",
            "text": "ThinkBook 16G", "segments": [],
        }))
        self.assertEqual(saved["jobs"], 2)
        for _ in range(2):
            job = asyncio.run(self.service._claim_job())
            self.assertIsNotNone(job)
            asyncio.run(self.service._process_job(job))

        message = self.client.get("/api/messages").json()["items"][0]
        self.assertEqual(message["result"], "review")
        self.assertEqual((message["review_count"], message["not_match_count"]), (1, 1))
        self.assertEqual(self.client.get("/api/messages", params={"q": "30001"}).json()["items"][0]["id"], message["id"])
        self.assertEqual(self.client.get("/api/messages", params={"result": "not_match"}).json()["items"], [])

        detail = self.client.get(f"/api/messages/{message['id']}").json()
        self.assertEqual({item["task_name"] for item in detail["evaluations"]}, {"目标任务", "无关任务"})
        lead = self.client.get("/api/leads", params={"q": "3000"}).json()["items"][0]
        self.assertEqual((lead["sender_id"], lead["sender_name"]), ("30001", "测试发布者"))

        asyncio.run(self.db.execute("DELETE FROM raw_messages WHERE id=?", (message["id"],)))
        retained = self.client.get("/api/leads", params={"q": "30001"}).json()["items"]
        self.assertEqual((len(retained), retained[0]["sender_id"]), (1, "30001"))

    def test_template_versioning_and_onboarding_contract(self):
        self.authenticate()
        template = self.client.get("/api/templates").json()["items"][0]
        self.assertEqual(template["name"], "电脑经销商模式")
        fields = template["fields"] + [{
            "key": "channel", "label": "渠道", "type": "text", "required": False,
            "show_in_list": True, "match": True,
        }]
        updated = self.client.patch(
            f"/api/templates/{template['id']}", headers=self.write_headers,
            json={"name": template["name"], "fields": fields, "version": template["version"]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["current_version"], 2)
        copied = self.client.post(
            f"/api/templates/{template['id']}/copy", headers=self.write_headers,
        )
        self.assertEqual(copied.status_code, 201, copied.text)
        custom = self.client.post(
            "/api/templates", headers=self.write_headers,
            json={"name": "渠道模式", "fields": [{
                "key": "channel", "label": "渠道", "type": "text", "required": True,
                "show_in_list": True, "match": True,
            }]},
        )
        self.assertEqual(custom.status_code, 201, custom.text)
        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        source = self.client.get("/api/sources").json()["items"][0]
        source = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": source["version"]},
        ).json()
        task = self.client.post(
            "/api/tasks", headers=self.write_headers,
            json={
                "name": "渠道任务", "template_id": custom.json()["id"], "template_version": 1,
                "conditions": {"aliases": ["显卡"], "alias_mode": "any", "source_ids": [source["id"]], "values": {"channel": "核心"}},
            },
        )
        self.assertEqual(task.status_code, 201, task.text)
        detail = self.client.get(f"/api/tasks/{task.json()['id']}").json()
        self.assertEqual(detail["values"]["channel"], "核心")
        onboarding = self.client.get("/api/onboarding").json()
        self.assertIn("access_token_configured", onboarding)
        self.assertNotIn("ONEBOT_ACCESS_TOKEN", str(onboarding))
        dismissed = self.client.patch(
            "/api/onboarding", headers=self.write_headers, json={"action": "dismiss"}
        )
        self.assertTrue(dismissed.json()["seen"])
        self.assertFalse(dismissed.json()["completed"])

    def test_generic_external_sqlite_import_and_publish(self):
        self.authenticate()
        template = self.client.post(
            "/api/templates", headers=self.write_headers,
            json={"name": "通用服务模式", "fields": [
                {"key": "service", "label": "服务", "type": "text", "required": True, "show_in_list": True, "match": False},
                {"key": "available_slots", "label": "可用数量", "type": "number", "required": False, "show_in_list": True, "match": False},
            ]},
        ).json()
        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        qq_source = self.client.get("/api/sources").json()["items"][0]
        qq_source = self.client.patch(
            f"/api/sources/{qq_source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": qq_source["version"]},
        ).json()

        external_path = self.settings.root / "other-app.db"
        connection = sqlite3.connect(external_path)
        try:
            connection.execute("CREATE TABLE stock(code TEXT PRIMARY KEY,title TEXT NOT NULL,short_title TEXT,quantity INTEGER NOT NULL)")
            connection.executemany(
                "INSERT INTO stock VALUES (?,?,?,?)",
                [("a-1", "品牌物品甲", "物品甲", 3), ("b-1", "品牌物品乙", "物品乙", 5)],
            )
            connection.commit()
        finally:
            connection.close()

        payload = {
            "name": "其他系统库存",
            "database_path": str(external_path),
            "query_text": "SELECT code,title,short_title,quantity FROM stock WHERE quantity>0",
            "key_column": "code",
            "template_id": template["id"],
            "field_mapping": {"service": "title", "available_slots": "quantity"},
            "defaults": {},
            "name_fields": ["service"],
            "alias_fields": ["service"],
            "weak_alias_fields": ["short_title"],
            "alias_mode": "any",
            "task_source_ids": [qq_source["id"]],
        }
        created = self.client.post("/api/external-sources", headers=self.write_headers, json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        source_id = created.json()["id"]
        preview = self.client.post(f"/api/external-sources/{source_id}/preview", headers=self.write_headers)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual((preview.json()["total"], preview.json()["new"]), (2, 2))
        self.assertEqual(preview.json()["sample"][0]["weak_aliases"], ["物品甲"])

        imported = self.client.post(f"/api/external-sources/{source_id}/imports", headers=self.write_headers)
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["created"], 2)
        preview = self.client.post(f"/api/external-sources/{source_id}/preview", headers=self.write_headers).json()
        self.assertEqual(preview["unchanged"], 2)

        published = self.client.post(f"/api/external-sources/{source_id}/publish", headers=self.write_headers)
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["published"], 2)
        records = self.client.get(f"/api/external-sources/{source_id}/records").json()["items"]
        self.assertTrue(all(record["task_id"] for record in records))
        tasks = self.client.get("/api/tasks").json()["items"]
        self.assertEqual({task["name"] for task in tasks}, {"品牌物品甲", "品牌物品乙"})
        self.assertEqual({task["values"]["available_slots"] for task in tasks}, {3, 5})
        task = next(item for item in tasks if item["name"] == "品牌物品甲")
        detail = self.client.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual(detail["conditions"]["weak_aliases"], ["物品甲"])

        connection = sqlite3.connect(external_path)
        try:
            connection.execute("UPDATE stock SET short_title='甲' WHERE code='a-1'")
            connection.commit()
        finally:
            connection.close()
        imported = self.client.post(f"/api/external-sources/{source_id}/imports", headers=self.write_headers)
        self.assertEqual(imported.json()["tasks_updated"], 1)
        detail = self.client.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual(detail["current_revision"], 2)
        self.assertEqual(detail["conditions"]["weak_aliases"], ["甲"])
        imported = self.client.post(f"/api/external-sources/{source_id}/imports", headers=self.write_headers)
        self.assertEqual(imported.json()["tasks_updated"], 0)

        asyncio.run(self.db.execute("UPDATE external_records SET active=0 WHERE task_id=?", (task["id"],)))
        listed = next(item for item in self.client.get("/api/tasks").json()["items"] if item["id"] == task["id"])
        self.assertEqual((listed["external_record_inactive"], listed["status"]), (1, "active"))

        unsafe = {**payload, "name": "非法查询", "query_text": "DELETE FROM stock"}
        response = self.client.post("/api/external-sources", headers=self.write_headers, json=unsafe)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unsafe_query")

    def test_replay_range_validation(self):
        self.authenticate()
        response = self.client.post(
            "/api/tasks/missing/replay-preview",
            headers=self.write_headers,
            json={"start_at": "2026-09-14T12:00:00+08:00", "end_at": "2026-09-14T11:00:00+08:00"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "validation_error")

        asyncio.run(self.service.connect_account("10001", "测试账号"))
        asyncio.run(self.service.refresh_sources("10001", [{"group_id": 20001, "group_name": "测试群"}]))
        source = self.client.get("/api/sources").json()["items"][0]
        source = self.client.patch(
            f"/api/sources/{source['id']}", headers=self.write_headers,
            json={"enabled": True, "version": source["version"]},
        ).json()
        task = self.client.post(
            "/api/tasks", headers=self.write_headers,
            json={
                "name": "重跑测试", "conditions": {
                    "direction": "purchase", "product": "办公本", "aliases": ["ThinkBook"],
                    "alias_mode": "any", "specs": {}, "price": {}, "quantity": {}, "trade": {},
                    "source_ids": [source["id"]],
                },
            },
        ).json()
        preview = self.client.post(
            f"/api/tasks/{task['id']}/replay-preview", headers=self.write_headers,
            json={"start_at": "2026-09-14T12:00:00+08:00", "end_at": "2026-09-14T13:00:00+08:00"},
        ).json()
        self.assertEqual(preview["start_at"], "2026-09-14T04:00:00+00:00")
        replay = self.client.post(
            f"/api/tasks/{task['id']}/replays", headers=self.write_headers,
            json={"start_at": "2026-09-14T12:00:00+08:00", "end_at": "2026-09-14T13:00:00+08:00"},
        ).json()
        self.assertEqual(replay["status"], "completed")


if __name__ == "__main__":
    unittest.main()
