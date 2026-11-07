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
from coeus.models import ArchiveJob, Camera, Event, YouTubeAccount
from coeus.recordings.library import library_root
from coeus.recordings import merge as mg
from coeus.recordings.synology import scan_segments
from coeus.youtube import uploader as yt

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube quota resets at midnight Pacific
_worker_lock = threading.Lock()


def recordings_root(db: Session, settings: Settings) -> Path:
    return library_root(settings.recordings_dir, str(ss.get(db, "library.root") or ""))


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
    segs = scan_segments(recordings_root(db, settings), camera_folder, day,
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


# --- Quota (per Google Cloud project = per OAuth client id) ----------------------------

def _today_pt() -> str:
    return datetime.now(PACIFIC).date().isoformat()


def quota_state(db: Session, account: YouTubeAccount) -> tuple[bool, int, int]:
    """(blocked, uploads_today, daily_limit) for the project the account belongs to."""
    entry = (ss.get(db, "youtube.quota") or {}).get(account.client_id, {})
    until = entry.get("blocked_until")
    blocked = bool(until and datetime.fromisoformat(until) > datetime.now(timezone.utc))
    count = entry.get("count", 0) if entry.get("date") == _today_pt() else 0
    return blocked, count, max(1, int(account.daily_upload_limit or 6))


def _update_quota(db: Session, client_id: str, bump: bool = False, block: bool = False) -> None:
    quota = dict(ss.get(db, "youtube.quota") or {})
    entry = dict(quota.get(client_id, {}))
    if entry.get("date") != _today_pt():
        entry = {"date": _today_pt(), "count": 0, "blocked_until": entry.get("blocked_until")}
    if bump:
        entry["count"] = entry.get("count", 0) + 1
    if block:
        now_pt = datetime.now(PACIFIC)
        reset = datetime.combine(now_pt.date() + timedelta(days=1), datetime.min.time(),
                                 tzinfo=PACIFIC) + timedelta(minutes=5)
        entry["blocked_until"] = reset.astimezone(timezone.utc).isoformat()
    quota[client_id] = entry
    ss.set_(db, "youtube.quota", quota)


def account_unavailable(db: Session, account: YouTubeAccount | None) -> str:
    """Why uploads for this account can't happen now ('' if they can)."""
    if account is None:
        return "no YouTube account selected for this camera"
    if not account.connected:
        return f"YouTube account “{account.name}” is not connected"
    blocked, count, cap = quota_state(db, account)
    if blocked:
        return f"waiting for YouTube quota reset ({account.name})"
    if count >= cap:
        return f"daily upload limit reached for “{account.name}” ({count}/{cap})"
    return ""


def _job_account(db: Session, job: ArchiveJob) -> tuple[Camera | None, YouTubeAccount | None]:
    cam = db.scalar(select(Camera).where(Camera.recording_folder == job.camera_folder))
    acct = db.get(YouTubeAccount, cam.yt_account_id) if cam and cam.yt_account_id else None
    return cam, acct


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
        job.status, job.attempts, job.error = "merging", job.attempts + 1, ""
    try:
        good, bad = mg.validate_segments(segs, settings.ffprobe)
        if not good:
            raise RuntimeError("no playable segments")
        mg.merge(good, out, settings.ffmpeg)
        note = (f"skipped {len(bad)} unreadable segment(s): "
                f"{', '.join(Path(b).name for b in bad)}") if bad else ""
        _set(job_id, status="merged", output_path=str(out), error=note)
    except Exception as err:  # noqa: BLE001
        log.exception("merge failed for job %s", job_id)
        _set(job_id, status="failed", error=f"merge: {err}")


def upload_job(settings: Settings, job_id: int) -> bool:
    """Upload one merged part. Returns False when its account can't upload right now."""
    with session_scope() as db:
        job = db.get(ArchiveJob, job_id)
        assert job is not None
        cam, acct = _job_account(db, job)
        why = account_unavailable(db, acct)
        if why:
            job.error = why
            return False
        account_id, client_id, creds = acct.id, acct.client_id, acct.credentials
        category = ss.get(db, "youtube.category_id") or "22"
        keep = ss.get(db, "archive.keep_merged")
        segs, gaps = job_segments(job)
        camera_name = cam.name if cam else job.camera_folder
        start = segs[0].start_dt if segs else None
        end = (segs[-1].start_dt + timedelta(seconds=segs[-1].duration)) if segs else None
        title = yt.render_title(cam.yt_title_template, camera=camera_name, date=job.day,
                                part=job.part, parts=job.parts,
                                start=start.strftime("%H:%M") if start else "",
                                end=end.strftime("%H:%M") if end else "")
        desc = yt.build_description(camera_name, job.day, job.part, job.parts,
                                    mg.chapters(segs), gaps, mg.fmt_ts)
        privacy = "private"  # archives are always private
        cam_id = cam.id
        playlist = cam.yt_playlist_id if cam.yt_playlist_account_id == account_id else ""
        resume_uri, output = job.upload_session_uri, Path(job.output_path)
        job.status, job.error = "uploading", ""

    def save_creds(info: dict) -> None:
        with session_scope() as db2:
            a = db2.get(YouTubeAccount, account_id)
            if a:
                a.credentials = info

    try:
        if not output.exists():
            raise FileNotFoundError(f"merged file missing: {output}")
        service = yt.build_service(creds, save_creds)
        video_id = yt.upload_video(service, output, title, desc, privacy, category, resume_uri,
                                   on_session=lambda uri: _set(job_id, upload_session_uri=uri))
        note, quota_hit = "", False
        try:
            add_to_camera_playlist(service, cam_id, account_id, camera_name, playlist, video_id)
        except yt.QuotaExceeded:
            # The video is already up: never fall through to the re-upload path.
            note, quota_hit = "uploaded; not added to the playlist (quota ran out)", True
        except Exception as err:  # noqa: BLE001
            log.warning("playlist step failed: %s", err)
            note = f"uploaded, but adding to the playlist failed: {err}"[:500]
        _set(job_id, status="done", video_id=video_id, upload_session_uri="", error=note)
        with session_scope() as db:
            _update_quota(db, client_id, bump=True, block=quota_hit)
        if keep == "delete":
            output.unlink(missing_ok=True)
        return True
    except yt.QuotaExceeded as err:
        log.warning("YouTube quota exceeded: %s", err)
        _set(job_id, status="merged", error="waiting for YouTube quota reset")
        with session_scope() as db:
            _update_quota(db, client_id, block=True)
        return False
    except yt.NotConnected as err:
        _set(job_id, status="merged", error=str(err))
        return False
    except Exception as err:  # noqa: BLE001
        log.exception("upload failed for job %s", job_id)
        _set(job_id, status="failed", error=f"upload: {err}")
        return True


def add_to_camera_playlist(service, cam_id: int, account_id: int, camera_name: str,
                           playlist: str, video_id: str) -> str:
    """Put the video in the camera's own private playlist, creating it the first time."""
    from googleapiclient.errors import HttpError

    def make() -> str:
        pid = yt.create_playlist(service, f"{camera_name} — Coeus archive",
                                 f"Daily recordings from the {camera_name} camera.")
        with session_scope() as db:
            c = db.get(Camera, cam_id)
            if c:
                c.yt_playlist_id, c.yt_playlist_account_id = pid, account_id
        return pid

    if not playlist:
        playlist = make()
    try:
        yt.add_to_playlist(service, playlist, video_id)
    except HttpError as err:
        if err.resp.status == 404:  # deleted on YouTube: make a fresh one
            playlist = make()
            yt.add_to_playlist(service, playlist, video_id)
        elif err.resp.status == 403 and yt._is_quota_error(err):
            raise yt.QuotaExceeded(str(err)) from err
        else:
            raise
    return playlist


def _next_job(db: Session, status: str) -> int | None:
    """Oldest job in `status` whose camera's YouTube account can upload now.

    Jobs for unavailable accounts are skipped (and get a note), so one exhausted account
    never blocks the others, and nothing is merged that can't be uploaded soon.
    """
    for job in db.scalars(select(ArchiveJob).where(ArchiveJob.status == status)
                          .order_by(ArchiveJob.day, ArchiveJob.part)):
        _, acct = _job_account(db, job)
        why = account_unavailable(db, acct)
        if not why:
            return job.id
        job.error = why
    return None


def run_worker(settings: Settings) -> None:
    """Merge and upload one part at a time (oldest day first)."""
    if not _worker_lock.acquire(blocking=False):
        return
    try:
        while True:
            _try_uploads(settings)  # finish already-merged parts first
            with session_scope() as db:
                job_id = _next_job(db, "pending")
            if job_id is None:
                break
            merge_job(settings, job_id)
            with session_scope() as db:
                job = db.get(ArchiveJob, job_id)  # may have been removed meanwhile
                merged = job is not None and job.status == "merged"
            if merged:
                upload_job(settings, job_id)  # right away, so merged files don't pile up
    finally:
        _worker_lock.release()


def _try_uploads(settings: Settings) -> None:
    while True:
        with session_scope() as db:
            job_id = _next_job(db, "merged")
        if job_id is None or not upload_job(settings, job_id):
            return


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
