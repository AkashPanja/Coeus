"""Bootstrap configuration (environment variables).

Everything else is configured in the web UI and stored in the database.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COEUS_", env_file=".env", extra="ignore")

    secret_key: str = "change-me"
    admin_user: str = "admin"
    admin_password: str = ""
    data_dir: Path = Path("/data")
    recordings_dir: Path = Path("/recordings")
    timezone: str = "Asia/Kolkata"
    session_max_age_days: int = 7
    # Base URL used when building links in notifications (e.g. https://nas.example:8765)
    public_url: str = ""
    # Disable background workers (tests / one-off CLI use).
    disable_workers: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'coeus.db'}"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.work_dir, self.media_dir, self.models_dir,
                  self.media_dir / "events", self.media_dir / "faces"):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
