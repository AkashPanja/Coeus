from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from coeus import settings_store as ss
from coeus.jobs import archive
from coeus.models import ArchiveJob, Camera, YouTubeAccount
from coeus.youtube import uploader as yt
from tests.conftest import HAS_FFMPEG, make_segment

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
DAY = date(2025, 5, 16)


def client(cid):
    return {"installed": {"client_id": cid, "client_secret": "s"}}


@pytest.fixture
def planned(db, settings, recordings):
    base = datetime(2025, 5, 16, 12, 27, 34)
    for i in range(3):
        make_segment(recordings, "Main Gate", base + timedelta(seconds=2 * i), seconds=2)
    acct = YouTubeAccount(name="Home", client_config=client("proj-a"), credentials={"t": 1})
    db.add(acct)
    db.flush()
    db.add(Camera(name="Main Gate", recording_folder="Main Gate", archive_enabled=True,
                  yt_title_template="{camera} {date} {start}-{end}", yt_privacy="private",
                  yt_account_id=acct.id))
    ss.set_(db, "archive.max_part_hours", 0.0013)  # 4.7 s parts -> 2 + 1 segments
    jobs = archive.plan_day(db, settings, "Main Gate", DAY)
    db.commit()
    return jobs


def test_plan_is_idempotent(planned, db, settings):
    assert [j.part for j in planned] == [1, 2] and planned[0].parts == 2
    again = archive.plan_day(db, settings, "Main Gate", DAY)
    assert sorted(j.id for j in again) == sorted(j.id for j in planned)


def test_worker_merges_uploads_and_cleans(planned, db, settings, monkeypatch):
    uploads = []
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: object())

    def fake_upload(service, path, title, desc, privacy, category, resume_uri, on_session=None,
                    on_progress=None):
        assert path.exists() and path.stat().st_size > 0
        on_session("https://upload/session/1")
        uploads.append((title, privacy))
        return f"vid{len(uploads)}"

    monkeypatch.setattr(yt, "upload_video", fake_upload)
    archive.run_worker(settings)
    db.expire_all()
    jobs = db.scalars(select(ArchiveJob).order_by(ArchiveJob.part)).all()
    assert [j.status for j in jobs] == ["done", "done"]
    assert [j.video_id for j in jobs] == ["vid1", "vid2"]
    assert uploads[0] == ("Main Gate 2025-05-16 12:27-12:27", "private")
    assert not any(settings.work_dir.rglob("*.mp4"))  # deleted after upload
    assert archive.quota_state(db, db.get(YouTubeAccount, 1))[1] == 2


def test_quota_exceeded_pauses_uploads(planned, db, settings, monkeypatch):
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: object())

    def boom(*a, **k):
        raise yt.QuotaExceeded("quotaExceeded")

    monkeypatch.setattr(yt, "upload_video", boom)
    archive.run_worker(settings)
    db.expire_all()
    statuses = [j.status for j in db.scalars(select(ArchiveJob).order_by(ArchiveJob.part))]
    assert statuses == ["merged", "pending"]  # stops merging too, so disk doesn't fill
    assert archive.quota_state(db, db.get(YouTubeAccount, 1))[0] is True


def test_recover_interrupted(planned, db):
    planned[0].status, planned[1].status = "merging", "uploading"
    db.commit()
    archive.recover_interrupted()
    db.expire_all()
    assert [j.status for j in db.scalars(select(ArchiveJob).order_by(ArchiveJob.part))] == [
        "pending", "merged"]


def test_exhausted_account_does_not_block_others(planned, db, settings, recordings,
                                                 monkeypatch):
    # Second camera on another Google project, plus a third account sharing project A.
    make_segment(recordings, "Main Road Cam", datetime(2025, 5, 16, 9, 0, 0), seconds=1)
    b = YouTubeAccount(name="Office", client_config=client("proj-b"), credentials={"t": 2})
    shared = YouTubeAccount(name="Home 2", client_config=client("proj-a"), credentials={"t": 3})
    db.add_all([b, shared])
    db.flush()
    db.add(Camera(name="Main Road Cam", recording_folder="Main Road Cam", archive_enabled=True,
                  yt_account_id=b.id))
    archive.plan_day(db, settings, "Main Road Cam", DAY)
    archive._update_quota(db, "proj-a", block=True)
    db.commit()
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: creds)
    used = []
    monkeypatch.setattr(yt, "upload_video",
                        lambda service, *a, **k: used.append(service["t"]) or "v")
    archive.run_worker(settings)
    db.expire_all()
    by_cam = {j.camera_folder: j.status for j in db.scalars(select(ArchiveJob))}
    assert by_cam["Main Road Cam"] == "done" and used == [2]
    gate = db.scalars(select(ArchiveJob).where(ArchiveJob.camera_folder == "Main Gate")).all()
    assert {j.status for j in gate} == {"pending"}  # not even merged while quota is out
    assert "quota" in gate[0].error
    assert archive.quota_state(db, db.get(YouTubeAccount, shared.id))[0] is True  # same project


def test_camera_without_account_is_skipped(planned, db, settings):
    cam = db.scalar(select(Camera))
    cam.yt_account_id = None
    db.commit()
    archive.run_worker(settings)
    db.expire_all()
    jobs = db.scalars(select(ArchiveJob)).all()
    assert {j.status for j in jobs} == {"pending"}
    assert "no YouTube account" in jobs[0].error


class FakeYT:
    """Stands in for the YouTube API client in playlist tests."""
    def __init__(self):
        self.playlists, self.items = [], []


def test_each_camera_gets_its_own_private_playlist(planned, db, settings, monkeypatch):
    api = FakeYT()
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: api)
    privacy = []
    monkeypatch.setattr(yt, "upload_video", lambda service, path, title, desc, priv, *a, **k:
                        privacy.append(priv) or f"v{len(privacy)}")
    monkeypatch.setattr(yt, "create_playlist", lambda service, title, desc="":
                        api.playlists.append(title) or f"PL{len(api.playlists)}")
    monkeypatch.setattr(yt, "add_to_playlist", lambda service, pl, vid: api.items.append((pl, vid)))
    cam = db.scalar(select(Camera))
    cam.yt_privacy = "public"  # ignored: archives are always private
    db.commit()
    archive.run_worker(settings)
    db.expire_all()
    cam = db.scalar(select(Camera))
    assert privacy == ["private", "private"]
    assert api.playlists == ["Main Gate — Coeus archive"]  # created once, then reused
    assert api.items == [("PL1", "v1"), ("PL1", "v2")]
    assert cam.yt_playlist_id == "PL1" and cam.yt_playlist_account_id == cam.yt_account_id


def test_playlist_quota_error_does_not_reupload(planned, db, settings, monkeypatch):
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: object())
    uploads = []
    monkeypatch.setattr(yt, "upload_video", lambda *a, **k: uploads.append(1) or "v1")

    def no_quota(*a, **k):
        raise yt.QuotaExceeded("quota")

    monkeypatch.setattr(yt, "create_playlist", no_quota)
    archive.run_worker(settings)
    db.expire_all()
    jobs = db.scalars(select(ArchiveJob).order_by(ArchiveJob.part)).all()
    assert uploads == [1] and jobs[0].status == "done" and "playlist" in jobs[0].error
    assert jobs[1].status == "pending"  # quota is out: stop for today
