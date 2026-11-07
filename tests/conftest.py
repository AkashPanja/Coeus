from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
IST = timezone(timedelta(hours=5, minutes=30))


def seg_name(camera: str, local: datetime, seq: int = 7) -> str:
    epoch_ms = int(local.replace(tzinfo=IST).timestamp() * 1000)
    return f"{camera}-{local:%Y%m%d}-{local:%H%M%S}-{epoch_ms}-{seq}.mp4"


def half_dir(local: datetime) -> str:
    return f"{local:%Y%m%d}{'AM' if local.hour < 12 else 'PM'}"


def make_segment(root: Path, camera: str, local: datetime, seconds: float = 0,
                 old: bool = True) -> Path:
    d = root / camera / half_dir(local)
    d.mkdir(parents=True, exist_ok=True)
    p = d / seg_name(camera, local)
    if seconds and HAS_FFMPEG:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=160x120:rate=10:duration={seconds}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(p)], check=True)
    else:
        p.write_bytes(b"\0" * 1024)
    if old:
        t = time.time() - 3600
        os.utime(p, (t, t))
    return p


@pytest.fixture
def recordings(tmp_path) -> Path:
    root = tmp_path / "recordings"
    (root / "@DetectionEvent").mkdir(parents=True)
    (root / "node").mkdir()
    (root / "xiaomi_camera_videos").mkdir()
    (root / "Main Gate" / "@SSRECMETA").mkdir(parents=True)
    (root / "Main Gate" / "cam_settings.conf").write_text("x")
    return root


@pytest.fixture
def settings(tmp_path, recordings):
    from coeus.config import Settings
    s = Settings(data_dir=tmp_path / "data", recordings_dir=recordings, secret_key="test",
                 admin_user="admin", admin_password="secret-pass", disable_workers=True,
                 timezone="Asia/Kolkata")
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings):
    from coeus.db import init_engine, session_scope
    init_engine(settings.db_url)
    with session_scope() as s:
        yield s
