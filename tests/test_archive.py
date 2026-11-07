from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from coeus import settings_store as ss
from coeus.jobs import archive
from coeus.models import ArchiveJob, Camera
from coeus.youtube import uploader as yt
from tests.conftest import HAS_FFMPEG, make_segment

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
DAY = date(2025, 5, 16)


@pytest.fixture
def planned(db, settings, recordings):
    base = datetime(2025, 5, 16, 12, 27, 34)
    for i in range(3):
        make_segment(recordings, "Main Gate", base + timedelta(seconds=2 * i), seconds=2)
    db.add(Camera(name="Main Gate", recording_folder="Main Gate", archive_enabled=True,
                  yt_title_template="{camera} {date} {start}-{end}", yt_privacy="private"))
    ss.set_(db, "archive.max_part_hours", 0.0013)  # 4.7 s parts -> 2 + 1 segments
    ss.set_(db, "youtube.credentials", {"token": "x"})
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
    assert archive._quota_state(db)[1] == 2


def test_quota_exceeded_pauses_uploads(planned, db, settings, monkeypatch):
    monkeypatch.setattr(yt, "build_service", lambda creds, cb=None: object())

    def boom(*a, **k):
        raise yt.QuotaExceeded("quotaExceeded")

    monkeypatch.setattr(yt, "upload_video", boom)
    archive.run_worker(settings)
    db.expire_all()
    statuses = [j.status for j in db.scalars(select(ArchiveJob).order_by(ArchiveJob.part))]
    assert statuses == ["merged", "pending"]  # stops merging too, so disk doesn't fill
    assert archive._quota_state(db)[0] is True


def test_recover_interrupted(planned, db):
    planned[0].status, planned[1].status = "merging", "uploading"
    db.commit()
    archive.recover_interrupted()
    db.expire_all()
    assert [j.status for j in db.scalars(select(ArchiveJob).order_by(ArchiveJob.part))] == [
        "pending", "merged"]
