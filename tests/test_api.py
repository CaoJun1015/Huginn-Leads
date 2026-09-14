import asyncio
from pathlib import Path
import tempfile
import unittest

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
