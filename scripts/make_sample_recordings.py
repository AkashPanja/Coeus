#!/usr/bin/env python3
"""Create a fake Synology Surveillance Station share for testing Coeus without a NAS.

    python scripts/make_sample_recordings.py ./sample-volume

Creates <dir>/surveillance/<Camera>/<YYYYMMDD>AM|PM/<Camera>-<date>-<time>-<epoch_ms>-7.mp4
with real, playable 5-second clips (needs ffmpeg), plus folders that are not cameras
(@DetectionEvent, docker, photo), so the folder picker has something to figure out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
CAMERAS = ["Main Gate", "Main Road Cam", "Hall 2 Second Floor"]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required (apt install ffmpeg / brew install ffmpeg / winget install ffmpeg)")
        return 1
    vol = Path(sys.argv[1]).resolve()
    share = vol / "surveillance"
    for extra in (share / "@DetectionEvent", vol / "docker", vol / "photo"):
        extra.mkdir(parents=True, exist_ok=True)
    day = (datetime.now(IST) - timedelta(days=1)).replace(hour=0, minute=27, second=34,
                                                           microsecond=0, tzinfo=None)
    for cam in CAMERAS:
        for i in range(6):  # six segments spread over yesterday
            start = day + timedelta(hours=4 * i)
            half = f"{start:%Y%m%d}{'AM' if start.hour < 12 else 'PM'}"
            d = share / cam / half
            d.mkdir(parents=True, exist_ok=True)
            epoch_ms = int(start.replace(tzinfo=IST).timestamp() * 1000)
            out = d / f"{cam}-{start:%Y%m%d}-{start:%H%M%S}-{epoch_ms}-7.mp4"
            if not out.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=5",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                                "-c:a", "aac", "-shortest", str(out)], check=True)
            old = time.time() - 3600  # look finished, not still recording
            os.utime(out, (old, old))
        (share / cam / "cam_settings.conf").write_text("sample\n")
    print(f"Sample share ready: {share}")
    print(f"Mount or point COEUS_RECORDINGS_DIR at: {vol}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
