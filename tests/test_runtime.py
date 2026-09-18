"""Exercise actual NoneBot dispatch in a subprocess, without sockets or real accounts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BODY = "UNIQUE_SYNTHETIC_BODY_71d2"
SENDER = "UNIQUE_SYNTHETIC_SENDER_81e3"


class RuntimeLogTests(unittest.TestCase):
    def test_receive_chain_keeps_body_out_of_stdout_and_stderr(self):
        root = Path(__file__).resolve().parents[1]
        environment = os.environ | {"PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"), str(root))), "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--receive-check"],
                                    cwd=directory, env=environment, capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for output in (result.stdout, result.stderr):
            self.assertNotIn(BODY, output)
            self.assertNotIn(SENDER, output)
        self.assertIn("message_saved account=10001", result.stdout)
        self.assertIn("kind=RuntimeError", result.stdout)
        self.assertIn("kind=ValidationError", result.stdout)
        self.assertIn("receive-check-ok", result.stdout)


async def receive_check():
    from unittest.mock import AsyncMock, patch
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, Bot
    from fastapi.testclient import TestClient
    from huginn_leads import runtime
    from huginn_leads.security import Security
    from test_service import ServiceTests

    case = ServiceTests()
    await case.asyncSetUp()
    try:
        await case.create_task()
        await case.service.refresh_sources("10001", [{"user_id": 30001}], "contact")
        nonebot.init(driver="~fastapi", log_level="INFO")
        nonebot.get_driver().register_adapter(Adapter)
        security = Security(case.db)
        security.setup_code = "synthetic-setup-code"
        with patch.object(runtime, "load_settings", return_value=case.settings), patch.object(runtime, "Database", return_value=case.db), patch.object(runtime, "Workbench", return_value=case.service), patch.object(runtime, "Security", return_value=security):
            runtime.install()
        bot = Bot(nonebot.get_adapter(Adapter), "10001")

        def event(identifier, source_type="group"):
            data = {"time": 1700000000, "self_id": 10001, "post_type": "message",
                    "message_type": source_type, "sub_type": "normal" if source_type == "group" else "friend",
                    "message_id": identifier, "user_id": 30001, "group_id": 20001,
                    "message": [{"type": "text", "data": {"text": BODY}}],
                    "raw_message": BODY, "font": 0, "sender": {"user_id": 30001, "nickname": SENDER}}
            return Adapter.json_to_event(data)

        for source_type in ("group", "private"):
            database_type = "group" if source_type == "group" else "contact"
            await case.db.execute("UPDATE sources SET enabled=1 WHERE source_type=?", (database_type,))
            await bot.handle_event(event(1, source_type))
            await bot.handle_event(event(1, source_type))  # duplicate
            await case.db.execute("UPDATE sources SET enabled=0 WHERE source_type=?", (database_type,))
            await bot.handle_event(event(2, source_type))  # disabled
        rows = await case.db.fetch_all("SELECT * FROM raw_messages")
        assert len(rows) == 2, "enabled and duplicate/disabled persistence counts"
        assert all(row["text"] == BODY and BODY in row["segments_json"] for row in rows), "stored evidence unchanged"
        assert (await case.db.fetch_one("SELECT COUNT(*) n FROM processing_jobs"))["n"] == 1
        await case.db.execute("UPDATE sources SET enabled=1")
        await case.service.disconnect_account("10001")
        await bot.handle_event(event(3))
        assert (await case.db.fetch_one("SELECT COUNT(*) n FROM raw_messages"))["n"] == 2
        # Both handler exceptions and adapter validation failures used to expose payloads.
        with patch.object(case.service, "ingest", new=AsyncMock(side_effect=RuntimeError(BODY + SENDER))):
            await bot.handle_event(event(4))
        assert Adapter.json_to_event({"post_type": "message", "time": BODY, "self_id": SENDER}) is None
        # The protected raw-message API still returns the complete stored text.
        client = TestClient(nonebot.get_app(), base_url=case.settings.origin)
        try:
            url = f"/api/messages/{rows[0]['id']}"
            assert client.get(url).status_code == 401
            response = client.post("/api/auth/setup", headers={"Origin": case.settings.origin},
                                   json={"code": security.setup_code, "password": "synthetic-password"})
            assert response.status_code == 200
            response = client.get(url)
            assert response.status_code == 200
            assert response.json()["text"] == BODY, "protected evidence must remain readable"
        finally:
            client.close()
    finally:
        await case.asyncTearDown()
    print("receive-check-ok")


if __name__ == "__main__":
    if "--receive-check" in sys.argv:
        import asyncio
        asyncio.run(receive_check())
    else:
        unittest.main()
