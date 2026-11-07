from datetime import date, datetime, timedelta

import pytest

from coeus.recordings import merge as mg
from coeus.recordings.synology import scan_segments
from tests.conftest import HAS_FFMPEG, make_segment


def _segs(recordings, starts):
    for t in starts:
        make_segment(recordings, "Main Gate", t)
    return scan_segments(recordings, "Main Gate", starts[0].date())


def test_plan_splits_at_max_length(recordings):
    base = datetime(2025, 5, 16, 0, 27, 34)
    segs = _segs(recordings, [base + timedelta(minutes=30 * i) for i in range(48)])
    parts = mg.plan_parts(segs, [1800.0] * 48, max_part_seconds=11.9 * 3600)
    assert [len(p.segments) for p in parts] == [16, 16, 16]
    assert all(p.duration <= 12 * 3600 for p in parts)


def test_plan_caps_at_youtube_limit(recordings):
    base = datetime(2025, 5, 16, 0, 0, 0)
    segs = _segs(recordings, [base + timedelta(minutes=30 * i) for i in range(30)])
    parts = mg.plan_parts(segs, [1800.0] * 30, max_part_seconds=48 * 3600)
    assert all(p.duration <= mg.YOUTUBE_MAX_SECONDS for p in parts)


def test_gaps_recorded_or_split(recordings):
    base = datetime(2025, 5, 16, 1, 0, 0)
    starts = [base, base + timedelta(minutes=30), base + timedelta(hours=3)]
    segs = _segs(recordings, starts)
    parts = mg.plan_parts(segs, [1800.0] * 3, 11 * 3600, gap_seconds=120)
    assert len(parts) == 1 and len(parts[0].gaps) == 1
    assert parts[0].gaps[0][1] == pytest.approx(2 * 3600)
    split = mg.plan_parts(segs, [1800.0] * 3, 11 * 3600, gap_seconds=120, split_on_gaps=True)
    assert [len(p.segments) for p in split] == [2, 1]


def test_duration_fallback_uses_next_start(recordings):
    base = datetime(2025, 5, 16, 1, 0, 0)
    segs = _segs(recordings, [base, base + timedelta(minutes=29)])
    assert mg.estimate_durations(segs, ffprobe=None) == [29 * 60, 1800.0]


def test_chapters_and_timestamps():
    segs = [mg.PlannedSegment("a", "2025-05-16T12:27:34", 1800),
            mg.PlannedSegment("b", "2025-05-16T12:57:34", 1800)]
    assert mg.chapters(segs) == [(0.0, "12:27:34"), (1800.0, "12:57:34")]
    assert mg.fmt_ts(0) == "00:00" and mg.fmt_ts(3725) == "1:02:05"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_real_lossless_merge(recordings, tmp_path):
    base = datetime(2025, 5, 16, 12, 27, 34)
    for i in range(3):
        make_segment(recordings, "Main Gate", base + timedelta(seconds=2 * i), seconds=2)
    segs = scan_segments(recordings, "Main Gate", date(2025, 5, 16))
    durs = mg.estimate_durations(segs)
    assert all(1.5 < d < 2.6 for d in durs)
    part = mg.plan_parts(segs, durs, 3600)[0]
    out = mg.merge(part.segments, tmp_path / "out" / "merged.mp4")
    total = mg.probe_duration(out)
    assert total == pytest.approx(sum(durs), abs=0.5)
    assert not (tmp_path / "out" / "merged.txt").exists()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_validate_skips_corrupt(recordings):
    good = make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 1, 0, 0), seconds=1)
    bad = make_segment(recordings, "Main Gate", datetime(2025, 5, 16, 1, 30, 0))  # junk bytes
    segs = [mg.PlannedSegment(str(good), "2025-05-16T01:00:00", 1),
            mg.PlannedSegment(str(bad), "2025-05-16T01:30:00", 1)]
    ok, rejected = mg.validate_segments(segs)
    assert [s.path for s in ok] == [str(good)] and rejected == [str(bad)]
