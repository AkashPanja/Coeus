"""Scanner for Synology Surveillance Station recording folders.

Layout (observed on DSM 7 / Surveillance Station 9):

    <share>/<Camera Name>/<YYYYMMDD><AM|PM>/<Camera Name>-<YYYYMMDD>-<HHMMSS>-<epoch_ms>-<n>.mp4
    <share>/<Camera Name>/@SSRECMETA/...         (internal, ignored)
    <share>/@DetectionEvent/...                  (internal, ignored)

The date/time in the filename is the NAS local time; epoch_ms is the exact UTC instant.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEGMENT_RE = re.compile(
    r"^(?P<camera>.+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<epoch>\d{10,13})-(?P<seq>\d+)"
    r"\.(?P<ext>mp4|mkv|avi|mov)$",
    re.IGNORECASE,
)
DAY_DIR_RE = re.compile(r"^(?P<date>\d{8})(?P<half>AM|PM)$", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}


@dataclass(frozen=True)
class Segment:
    path: Path
    camera: str
    local_start: datetime  # naive, NAS local time
    utc_start: datetime    # aware UTC (from epoch)
    seq: int
    size: int
    mtime: float

    @property
    def day(self) -> date:
        return self.local_start.date()


@dataclass
class DaySummary:
    day: date
    segments: list[Segment] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def size(self) -> int:
        return sum(s.size for s in self.segments)

    @property
    def first(self) -> datetime | None:
        return self.segments[0].local_start if self.segments else None

    @property
    def last(self) -> datetime | None:
        return self.segments[-1].local_start if self.segments else None


def parse_segment_name(name: str) -> dict | None:
    m = SEGMENT_RE.match(name)
    if not m:
        return None
    try:
        local = datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    epoch = int(m["epoch"])
    if len(m["epoch"]) == 13:
        epoch_s = epoch / 1000
    else:
        epoch_s = float(epoch)
    return {
        "camera": m["camera"],
        "local_start": local,
        "utc_start": datetime.fromtimestamp(epoch_s, tz=timezone.utc),
        "seq": int(m["seq"]),
    }


def is_camera_folder(path: Path) -> bool:
    """A camera folder contains YYYYMMDDAM/PM subfolders (or segment files directly)."""
    if not path.is_dir() or path.name.startswith(("@", ".", "#")):
        return False
    try:
        for child in path.iterdir():
            if child.is_dir() and DAY_DIR_RE.match(child.name):
                return True
            if child.is_file() and parse_segment_name(child.name):
                return True
    except PermissionError:
        return False
    return False


def list_camera_folders(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if is_camera_folder(p))


def _day_dirs(cam_dir: Path, day: date | None = None) -> list[Path]:
    out = []
    for child in cam_dir.iterdir():
        if not child.is_dir():
            continue
        m = DAY_DIR_RE.match(child.name)
        if not m:
            continue
        if day is not None:
            d = datetime.strptime(m["date"], "%Y%m%d").date()
            # A segment that starts just before midnight lives in the previous
            # day's PM folder, so look at neighbours too and filter by start time.
            if abs((d - day).days) > 1:
                continue
        out.append(child)
    return out


def scan_segments(root: Path, camera_folder: str, day: date | None = None,
                  settle_seconds: float = 300, now: float | None = None) -> list[Segment]:
    """Return finished segments for a camera (optionally one local day), sorted by start."""
    cam_dir = root / camera_folder
    if not cam_dir.is_dir():
        return []
    now = time.time() if now is None else now
    files: list[Path] = [p for p in cam_dir.iterdir() if p.is_file()]
    for d in _day_dirs(cam_dir, day):
        files.extend(p for p in d.iterdir() if p.is_file())

    segs: list[Segment] = []
    for p in files:
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        info = parse_segment_name(p.name)
        if info is None:
            continue
        if day is not None and info["local_start"].date() != day:
            continue
        st = p.stat()
        segs.append(Segment(path=p, camera=info["camera"], local_start=info["local_start"],
                            utc_start=info["utc_start"], seq=info["seq"], size=st.st_size,
                            mtime=st.st_mtime))
    segs.sort(key=lambda s: (s.utc_start, s.seq))
    # Drop a trailing segment that may still be recording.
    if segs and now - segs[-1].mtime < settle_seconds:
        segs = segs[:-1]
    return segs


def summarize_days(root: Path, camera_folder: str, settle_seconds: float = 300) -> list[DaySummary]:
    by_day: dict[date, DaySummary] = {}
    for seg in scan_segments(root, camera_folder, None, settle_seconds):
        by_day.setdefault(seg.day, DaySummary(seg.day)).segments.append(seg)
    return [by_day[d] for d in sorted(by_day, reverse=True)]


def yesterday(tz_now: datetime) -> date:
    return (tz_now - timedelta(days=1)).date()
