from datetime import date, datetime

from coeus.recordings.synology import (list_camera_folders, parse_segment_name, scan_segments,
                                       summarize_days)
from tests.conftest import make_segment


def test_parse_real_filename():
    info = parse_segment_name("Main Gate-20250516-122734-1747378654295-7.mp4")
    assert info["camera"] == "Main Gate"
    assert info["local_start"] == datetime(2025, 5, 16, 12, 27, 34)
    # epoch is UTC: 12:27:34 IST == 06:57:34 UTC
    assert info["utc_start"].strftime("%H:%M:%S") == "06:57:34"
    assert info["seq"] == 7


def test_parse_camera_with_hyphen_and_rejects_junk():
    assert parse_segment_name("Cam-A-B-20250516-010203-1747337523000-1.mp4")["camera"] == "Cam-A-B"
    assert parse_segment_name("cam_settings.conf") is None
    assert parse_segment_name("Main Gate-2025-05-16.mp4") is None


def test_lists_only_camera_folders(recordings):
    make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 12, 27, 34))
    make_segment(recordings, "Main Road Cam", datetime(2025, 5, 16, 0, 27, 34))
    assert list_camera_folders(recordings) == ["Main Gate", "Main Road Cam"]


def test_scan_day_orders_and_includes_cross_folder(recordings):
    times = [datetime(2025, 5, 16, h, 27, 34) for h in (13, 0, 12, 11)]
    for t in times:
        make_segment(recordings, "Main Gate", t)
    make_segment(recordings, "Main Gate", datetime(2025, 5, 15, 23, 57, 34))  # previous day
    make_segment(recordings, "Main Gate", datetime(2025, 5, 17, 0, 27, 34))   # next day
    segs = scan_segments(recordings, "Main Gate", date(2025, 5, 16))
    assert [s.local_start.hour for s in segs] == [0, 11, 12, 13]


def test_skips_segment_still_recording(recordings):
    make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 1, 0, 0))
    make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 1, 30, 0), old=False)
    segs = scan_segments(recordings, "Main Gate", date(2025, 5, 16), settle_seconds=300)
    assert len(segs) == 1


def test_summarize_days(recordings):
    for d in (15, 16):
        for h in (1, 13):
            make_segment(recordings, "Main Gate", datetime(2025, 5, d, h, 27, 34))
    days = summarize_days(recordings, "Main Gate")
    assert [d.day for d in days] == [date(2025, 5, 16), date(2025, 5, 15)]
    assert days[0].count == 2 and days[0].size == 2048
