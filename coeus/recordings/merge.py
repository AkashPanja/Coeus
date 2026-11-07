"""Plan and execute lossless merges of recording segments with ffmpeg."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from coeus.recordings.synology import Segment

log = logging.getLogger(__name__)

YOUTUBE_MAX_SECONDS = 12 * 3600


@dataclass
class PlannedSegment:
    path: str
    start: str       # ISO local time
    duration: float  # seconds

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)


@dataclass
class Part:
    index: int
    segments: list[PlannedSegment] = field(default_factory=list)
    gaps: list[tuple[str, float]] = field(default_factory=list)  # (after_time, gap_seconds)

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


def probe_duration(path: Path, ffprobe: str = "ffprobe", timeout: int = 60) -> float | None:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.SubprocessError, KeyError, ValueError, OSError):
        return None


def estimate_durations(segments: list[Segment], ffprobe: str | None = "ffprobe",
                       default: float = 1800.0) -> list[float]:
    """ffprobe each segment; fall back to the gap to the next segment's start."""
    durations: list[float] = []
    for i, seg in enumerate(segments):
        d = probe_duration(seg.path, ffprobe) if ffprobe else None
        if d is None or d <= 0:
            if i + 1 < len(segments):
                d = (segments[i + 1].utc_start - seg.utc_start).total_seconds()
                d = d if 0 < d <= 4 * 3600 else default
            else:
                d = default
        durations.append(d)
    return durations


def _greedy(durs: list[float], limit: float) -> list[int]:
    """Indices where a new part starts (always includes 0)."""
    cuts, acc = [0], 0.0
    for i, d in enumerate(durs):
        if i and acc + d > limit:
            cuts.append(i)
            acc = 0.0
        acc += d
    return cuts


def _balanced_cuts(durs: list[float], max_len: float) -> list[int]:
    """Fewest parts that fit max_len, with lengths as even as possible."""
    n = len(_greedy(durs, max_len))
    lo, hi = max(sum(durs) / n, max(durs)), max_len
    for _ in range(40):  # binary-search the smallest limit that still gives n parts
        mid = (lo + hi) / 2
        if len(_greedy(durs, mid)) <= n:
            hi = mid
        else:
            lo = mid
    return _greedy(durs, hi)


def plan_parts(segments: list[Segment], durations: list[float], max_part_seconds: float,
               gap_seconds: float = 120, split_on_gaps: bool = False) -> list[Part]:
    """Group consecutive segments into evenly sized parts no longer than max_part_seconds."""
    if len(segments) != len(durations):
        raise ValueError("segments and durations differ in length")
    max_part_seconds = min(max_part_seconds, YOUTUBE_MAX_SECONDS - 60)
    # 1. Gaps between consecutive segments.
    gaps: list[float] = [0.0]
    for prev, prev_d, seg in zip(segments, durations, segments[1:]):
        gaps.append((seg.local_start - (prev.local_start + timedelta(seconds=prev_d)))
                    .total_seconds())
    # 2. Runs that must not be joined (only when splitting on gaps).
    runs: list[list[int]] = [[]]
    for i in range(len(segments)):
        if i and split_on_gaps and gaps[i] > gap_seconds:
            runs.append([])
        runs[-1].append(i)
    # 3. Balanced split of each run.
    parts: list[Part] = []
    for run in (r for r in runs if r):
        cuts = _balanced_cuts([durations[i] for i in run], max_part_seconds) + [len(run)]
        for a, b in zip(cuts, cuts[1:]):
            part = Part(index=len(parts) + 1)
            for k, i in enumerate(run[a:b]):
                seg, dur = segments[i], durations[i]
                if k and gaps[i] > gap_seconds:
                    prev_end = segments[i - 1].local_start + timedelta(seconds=durations[i - 1])
                    part.gaps.append((prev_end.isoformat(timespec="seconds"), gaps[i]))
                part.segments.append(PlannedSegment(str(seg.path), seg.local_start.isoformat(),
                                                    dur))
            parts.append(part)
    return parts


def chapters(segs: list[PlannedSegment]) -> list[tuple[float, str]]:
    """(offset_seconds, label) for each segment, labelled by wall-clock start."""
    out, offset = [], 0.0
    for s in segs:
        out.append((offset, s.start_dt.strftime("%H:%M:%S")))
        offset += s.duration
    return out


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def write_concat_list(segs: list[PlannedSegment], list_path: Path) -> None:
    def esc(p: str) -> str:
        return p.replace("'", "'\\''")
    list_path.write_text("".join(f"file '{esc(s.path)}'\n" for s in segs), encoding="utf-8")


def check_free_space(out_dir: Path, segs: list[PlannedSegment], margin: float = 1.1) -> None:
    needed = sum(Path(s.path).stat().st_size for s in segs) * margin
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        raise OSError(f"Not enough free space in {out_dir}: need {needed/1e9:.1f} GB, "
                      f"have {free/1e9:.1f} GB")


def merge(segs: list[PlannedSegment], output: Path, ffmpeg: str = "ffmpeg",
          timeout: float | None = None) -> Path:
    """Concatenate segments without re-encoding. Corrupt segments are skipped."""
    output.parent.mkdir(parents=True, exist_ok=True)
    check_free_space(output.parent, segs)
    list_path = output.with_suffix(".txt")
    tmp = output.with_suffix(".partial" + output.suffix)
    write_concat_list(segs, list_path)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
           "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
           str(tmp)]
    log.info("merging %d segments -> %s", len(segs), output)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    list_path.unlink(missing_ok=True)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-2000:]}")
    tmp.replace(output)
    return output


def validate_segments(segs: list[PlannedSegment], ffprobe: str = "ffprobe") -> tuple[
        list[PlannedSegment], list[str]]:
    """Split segments into (playable, rejected-paths) using ffprobe."""
    good, bad = [], []
    for s in segs:
        if probe_duration(Path(s.path), ffprobe) is None:
            bad.append(s.path)
        else:
            good.append(s)
    return good, bad
