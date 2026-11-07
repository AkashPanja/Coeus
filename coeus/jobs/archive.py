"""Daily archive jobs: plan -> merge (lossless) -> upload to YouTube."""
from __future__ import annotations

import logging
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from coeus import settings_store as ss
from coeus.config import Settings
from coeus.db import session_scope
from coeus.models import ArchiveJob, Camera, Event
from coeus.recordings import merge as mg
from coeus.recordings.synology import scan_segments
from coeus.youtube import uploader as yt

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube quota resets at midnight Pacific
_worker_lock = threading.Lock()


def plan_day(db: Session, settings: Settings, camera_folder: str, day: date,
             probe: bool = True, replace: bool = False) -> list[ArchiveJob]:
    """Create (idempotently) one ArchiveJob per part for a camera/day."""
    existing = db.scalars(select(ArchiveJob).where(ArchiveJob.camera_folder == camera_folder,
                                                   ArchiveJob.day == day.isoformat())).all()
    if existing and not replace:
        return list(existing)
    if replace:
        for j in existing:
            if j.status in ("done", "uploading", "merging"):
                raise ValueError(f"part {j.part} is {j.status}; cannot re-plan")
            db.delete(j)
        db.flush()
    segs = scan_segments(settings.recordings_dir, camera_folder, day,
                         settle_seconds=float(ss.get(db, "archive.settle_minutes")) * 60)
    if not segs:
        return []
    durations = mg.estimate_durations(segs, settings.ffprobe if probe else None)
    parts = mg.plan_parts(segs, durations,
                          max_part_seconds=float(ss.get(db, "archive.max_part_hours")) * 3600,
                          gap_seconds=float(ss.get(db, "archive.gap_minutes")) * 60,
                          split_on_gaps=bool(ss.get(db, "archive.split_on_gaps")))
    jobs = []
    for part in parts:
        job = ArchiveJob(camera_folder=camera_folder, day=day.isoformat(), part=part.index,
                         parts=len(parts), duration_s=part.duration,
                         segments=[{"path": s.path, "start": s.start, "duration": s.duration}
                                   for s in part.segments],
                         gaps=[list(g) for g in part.gaps], error="", status="pending")
        db.add(job)
        jobs.append(job)
    db.flush()
    return jobs


def job_segments(job: ArchiveJob) -> tuple[list[mg.PlannedSegment], list]:
    segs = [mg.PlannedSegment(i["path"], i["start"], i["duration"]) for i in job.segments or []]
    return segs, job.gaps or []


def plan_yesterday(settings: Settings) -> int:
    tz = ZoneInfo(settings.timezone)
    day = (datetime.now(tz) - timedelta(days=1)).date()
    n = 0
    with session_scope() as db:
        for cam in db.scalars(select(Camera).where(Camera.archive_enabled.is_(True))):
            if cam.recording_folder:
                n += len(plan_day(db, settings, cam.recording_folder, day))
    log.info("planned %d archive parts for %s", n, day)
    return n


def recover_interrupted() -> None:
    """After a restart: merges start over, uploads resume."""
    with session_scope() as db:
        for job in db.scalars(select(ArchiveJob).where(
                ArchiveJob.status.in_(("merging", "uploading")))):
            job.status = "pending" if job.status == "merging" else "merged"


def _quota_state(db: Session) -> tuple[bool, int, int]:
    """(blocked, uploads_today, limit)"""
    today = datetime.now(PACIFIC).date().isoformat()
    blocked_until = ss.get(db, "youtube.quota_blocked_until")
    blocked = bool(blocked_until and datetime.fromisoformat(blocked_until) > datetime.now(timezone.utc))
    counter = ss.get(db, "youtube.uploads_today") or {}
    count = counter.get("count", 0) if counter.get("date") == today else 0
    return blocked, count, int(ss.get(db, "youtube.daily_upload_limit") or 6)


def _bump_upload_count(db: Session) -> None:
    today = datetime.now(PACIFIC).date().isoformat()
    counter = ss.get(db, "youtube.uploads_today") or {}
    count = counter.get("count", 0) if counter.get("date") == today else 0
    ss.set_(db, "youtube.uploads_today", {"date": today, "count": count + 1})


def _block_until_quota_reset(db: Session) -> None:
    now_pt = datetime.now(PACIFIC)
    reset = datetime.combine(now_pt.date() + timedelta(days=1), datetime.min.time(),
                             tzinfo=PACIFIC) + timedelta(minutes=5)
    ss.set_(db, "youtube.quota_blocked_until", reset.astimezone(timezone.utc).isoformat())


def _set(job_id: int, **fields) -> None:
    with session_scope() as db:
        job = db.get(ArchiveJob, job_id)
        if job:
            for k, v in fields.items():
                setattr(job, k, v)


def merge_job(settings: Settings, job_id: int) -> None:
    with session_scope() as db:
        job = db.get(ArchiveJob, job_id)
        assert job is not None
        segs, _ = job_segments(job)
        out = (settings.work_dir / job.camera_folder.replace("/", "_") /
               f"{job.day}-part{job.part}.mp4")
        job.status, job.attempts = "merging", job.attempts + 1
    try:
        good, bad = mg.validate_segments(segs, settings.ffprobe)
        if not good:
            raise RuntimeError("no playable segments")
        mg.merge(good, out, settings.ffmpeg)
        note = f"skipped {len(bad)} unreadable segment(s): {', '.join(Path(b).name for b in bad)}" if bad else ""
        _set(job_id, status="merged", output_path=str(out), error=note)
    except Exception as err:  # noqa: BLE001
        log.exception("merge failed for job %s", job_id)
        _set(job_id, status="failed", error=f"merge: {err}")


def upload_job(settings: Settings, job_id: int) -> bool:
    """Returns False when uploads must stop for now (quota)."""
    with session_scope() as db:
        job = db.get(ArchiveJob, job_id)
        assert job is not None
        cam = db.scalar(select(Camera).where(Camera.recording_folder == job.camera_folder))
        creds = ss.get(db, "youtube.credentials")
        category = ss.get(db, "youtube.category_id") or "22"
        keep = ss.get(db, "archive.keep_merged")
        segs, gaps = job_segments(job)
        camera_name = cam.name if cam else job.camera_folder
        title_tpl = cam.yt_title_template if cam else "{camera} — {date} (part {part}/{parts})"
        privacy = cam.yt_privacy if cam else "private"
        playlist = cam.yt_playlist_id if cam else ""
        start = segs[0].start_dt if segs else None
        end = (segs[-1].start_dt + timedelta(seconds=segs[-1].duration)) if segs else None
        title = yt.render_title(title_tpl, camera=camera_name, date=job.day, part=job.part,
                                parts=job.parts, start=start.strftime("%H:%M") if start else "",
                                end=end.strftime("%H:%M") if end else "")
        desc = yt.build_description(camera_name, job.day, job.part, job.parts,
                                    mg.chapters(segs), gaps, mg.fmt_ts)
        resume_uri, output = job.upload_session_uri, Path(job.output_path)
        job.status = "uploading"

    def save_creds(info: dict) -> None:
        with session_scope() as db2:
            ss.set_(db2, "youtube.credentials", info)

    try:
        if not output.exists():
            raise FileNotFoundError(f"merged file missing: {output}")
        service = yt.build_service(creds, save_creds)
        video_id = yt.upload_video(service, output, title, desc, privacy, category, resume_uri,
                                   on_session=lambda uri: _set(job_id, upload_session_uri=uri))
        if playlist:
            try:
                yt.add_to_playlist(service, playlist, video_id)
            except Exception as err:  # noqa: BLE001
                log.warning("playlist insert failed: %s", err)
        _set(job_id, status="done", video_id=video_id, upload_session_uri="")
        with session_scope() as db:
            _bump_upload_count(db)
        if keep == "delete":
            output.unlink(missing_ok=True)
        return True
    except yt.QuotaExceeded as err:
        log.warning("YouTube quota exceeded: %s", err)
        _set(job_id, status="merged", error="waiting for YouTube quota reset")
        with session_scope() as db:
            _block_until_quota_reset(db)
        return False
    except yt.NotConnected as err:
        _set(job_id, status="merged", error=str(err))
        return False
    except Exception as err:  # noqa: BLE001
        log.exception("upload failed for job %s", job_id)
        _set(job_id, status="failed", error=f"upload: {err}")
        return True


def run_worker(settings: Settings) -> None:
    """Process pending merges and uploads one at a time (oldest day first)."""
    if not _worker_lock.acquire(blocking=False):
        return
    try:
        while True:
            with session_scope() as db:
                job = db.scalar(select(ArchiveJob).where(ArchiveJob.status == "pending")
                                .order_by(ArchiveJob.day, ArchiveJob.part).limit(1))
                job_id = job.id if job else None
            if job_id is None:
                break
            merge_job(settings, job_id)
            # Upload right away so merged files don't pile up on disk.
            if not _try_uploads(settings, limit=1):
                break
        _try_uploads(settings)
    finally:
        _worker_lock.release()


def _try_uploads(settings: Settings, limit: int | None = None) -> bool:
    done = 0
    while limit is None or done < limit:
        with session_scope() as db:
            blocked, count, cap = _quota_state(db)
            if blocked or count >= cap or not ss.get(db, "youtube.credentials"):
                return False
            job = db.scalar(select(ArchiveJob).where(ArchiveJob.status == "merged")
                            .order_by(ArchiveJob.day, ArchiveJob.part).limit(1))
            job_id = job.id if job else None
        if job_id is None:
            return True
        if not upload_job(settings, job_id):
            return False
        done += 1
    return True


def cleanup_events(settings: Settings) -> int:
    with session_scope() as db:
        days = int(ss.get(db, "events.retention_days") or 30)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        old = db.scalars(select(Event).where(Event.ts < cutoff)).all()
        for ev in old:
            for p in (ev.snapshot_path, ev.face_path):
                if p:
                    Path(p).unlink(missing_ok=True)
            db.delete(ev)
    events_root = settings.media_dir / "events"
    for d in events_root.glob("*"):
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    return len(old)
