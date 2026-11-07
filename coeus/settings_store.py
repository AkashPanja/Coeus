"""Key/value settings stored in the DB, with defaults."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from coeus.models import Setting

DEFAULTS: dict[str, Any] = {
    # Archive
    "archive.schedule_time": "01:30",
    "archive.max_part_hours": 11.9,
    "archive.gap_minutes": 2,
    "archive.split_on_gaps": False,
    "archive.keep_merged": "delete",  # delete|keep
    "archive.settle_minutes": 5,
    # YouTube
    "youtube.client_config": None,  # OAuth client JSON (desktop app)
    "youtube.credentials": None,    # authorized-user JSON (refresh token)
    "youtube.category_id": "22",
    "youtube.daily_upload_limit": 6,
    "youtube.quota_blocked_until": None,
    # Face recognition
    "faces.threshold": 0.363,
    "faces.dedupe_seconds": 60,
    # LLM
    "llm.provider": "none",  # none|anthropic|openai
    "llm.model": "",
    "llm.api_key": "",
    "llm.base_url": "",
    "llm.max_calls_per_hour": 60,
    # Home Assistant
    "ha.url": "",
    "ha.token": "",
    # Events
    "events.retention_days": 30,
}

SECRET_KEYS = {"llm.api_key", "ha.token", "youtube.credentials", "youtube.client_config"}


def get(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return row.value


def set_(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def mask(value: Any) -> str:
    if not value:
        return ""
    s = str(value)
    return "•" * 8 + s[-4:] if len(s) > 8 else "•" * 8
