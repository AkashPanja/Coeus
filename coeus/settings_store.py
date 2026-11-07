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
    # Recordings library: folder (relative to the mounted path) that holds the camera folders
    "library.root": "",
    # YouTube (accounts live in their own table; quota state is keyed by OAuth client id)
    "youtube.category_id": "22",
    "youtube.quota": {},  # {client_id: {"date": ..., "count": n, "blocked_until": iso}}
    # Live watching: how many cameras may be analysed continuously (NAS CPU budget)
    "live.max_cameras": 1,
    # Gate: one camera that opens a gate for allowed people (see coeus/gate.py)
    "gate": {},
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

SECRET_KEYS = {"llm.api_key", "ha.token"}


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
