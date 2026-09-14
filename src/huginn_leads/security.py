from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from .database import Database, utcnow


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_value, digest_value = stored.split("$", 2)
        candidate = _password_hash(password, base64.urlsafe_b64decode(salt_value)).split("$", 2)[2]
        return hmac.compare_digest(candidate, digest_value)
    except (ValueError, TypeError):
        return False


class Security:
    def __init__(self, database: Database):
        self.database = database
        self.setup_code: str | None = None

    async def start(self) -> None:
        if not await self.is_initialized():
            self.setup_code = secrets.token_urlsafe(12)
            print(f"Huginn Leads initialization code: {self.setup_code}", flush=True)
        await self.database.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))

    async def is_initialized(self) -> bool:
        return bool(await self.database.get_setting("admin_password"))

    async def setup(self, code: str, password: str) -> None:
        if await self.is_initialized():
            raise ValueError("工作台已经初始化")
        if not self.setup_code or not hmac.compare_digest(code, self.setup_code):
            raise ValueError("初始化码无效")
        if len(password) < 12:
            raise ValueError("密码至少需要 12 个字符")
        await self.database.set_setting("admin_password", _password_hash(password))
        self.setup_code = None

    async def login(self, password: str) -> tuple[str, str]:
        stored = await self.database.get_setting("admin_password")
        if not stored or not _verify_password(password, stored):
            raise ValueError("密码错误")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        csrf = secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
        await self.database.execute(
            "INSERT INTO sessions(token_hash, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, csrf, utcnow(), expires_at),
        )
        return token, csrf

    async def session(self, token: str | None) -> dict[str, str] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await self.database.fetch_one(
            "SELECT token_hash, csrf_token, expires_at FROM sessions WHERE token_hash=? AND expires_at>?",
            (token_hash, utcnow()),
        )
        return row  # type: ignore[return-value]

    async def logout(self, token: str | None) -> None:
        if token:
            await self.database.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
