from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    backup_dir: Path
    database_path: Path
    static_dir: Path
    origin: str
    raw_days: int = 7
    lead_days: int = 30
    diagnostic_days: int = 7
    backup_keep: int = 7
    max_event_bytes: int = 1024 * 1024
    max_active_jobs: int = 10_000
    page_size: int = 50
    max_page_size: int = 200
    onebot_token_configured: bool = False


def load_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "18080"))
    data_dir = root / "data"
    backup_dir = root / "backups"
    env_file = dotenv_values(root / ".env")
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        root=root,
        data_dir=data_dir,
        backup_dir=backup_dir,
        database_path=data_dir / "huginn-leads.db",
        static_dir=root / "src" / "huginn_leads" / "static",
        origin=f"http://{host}:{port}",
        onebot_token_configured=bool(os.getenv("ONEBOT_ACCESS_TOKEN") or env_file.get("ONEBOT_ACCESS_TOKEN")),
    )
