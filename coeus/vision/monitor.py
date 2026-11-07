"""One background thread per camera: RTSP -> motion in zones -> faces -> events."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from coeus.vision.faces import Gallery, Match, crop
from coeus.vision.motion import MotionDetector, ZoneSpec, zone_bbox

log = logging.getLogger(__name__)

# Prefer TCP for RTSP (far fewer smeared frames over Wi-Fi) and keep latency low.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer")


@dataclass
class CameraConfig:
    id: int
    name: str
    url: str
    zones: list[ZoneSpec]
    detect_fps: float = 5.0
    face_mode: str = "local"  # local|llm|local+llm|off
    cooldown_s: int = 30


@dataclass
class Detection:
    camera: CameraConfig
    zone: str
    event_type: str  # motion|face_known|face_unknown
    frame: np.ndarray
    face_crop: np.ndarray | None = None
    match: Match | None = None
    motion_ratio: float = 0.0


@dataclass
class MonitorStatus:
    state: str = "stopped"  # stopped|connecting|online|reconnecting|error
    error: str = ""
    fps: float = 0.0
    last_frame_at: float = 0.0
    last_event_at: float = 0.0
    frames: int = 0
    extra: dict = field(default_factory=dict)


class CameraMonitor(threading.Thread):
    def __init__(self, cfg: CameraConfig, gallery: Gallery, face_backend_getter: Callable,
                 on_detection: Callable[[Detection], None], threshold_getter: Callable[[], float],
                 dedupe_seconds: float = 60.0, capture_factory: Callable | None = None):
        super().__init__(daemon=True, name=f"cam-{cfg.id}")
        self.cfg = cfg
        self.gallery = gallery
        self.face_backend_getter = face_backend_getter
        self.on_detection = on_detection
        self.threshold_getter = threshold_getter
        self.dedupe_seconds = dedupe_seconds
        self.capture_factory = capture_factory or (lambda url: cv2.VideoCapture(url, cv2.CAP_FFMPEG))
        self.status = MonitorStatus()
        self._stop = threading.Event()
        self._latest_jpeg: bytes | None = None
        self._latest_frame: np.ndarray | None = None
        self._latest_frame_at = 0.0
        self._latest_lock = threading.Lock()
        self._last_motion_event = 0.0
        self._last_person_seen: dict[int | str, float] = {}

    # -- public --------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def latest_jpeg(self) -> bytes | None:
        with self._latest_lock:
            return self._latest_jpeg

    def latest_frame(self) -> tuple[np.ndarray | None, float]:
        """Most recent decoded frame and its time.monotonic() timestamp."""
        with self._latest_lock:
            return self._latest_frame, self._latest_frame_at

    # -- loop ------------------------------------------------------------------
    def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            self.status.state = "connecting" if self.status.frames == 0 else "reconnecting"
            cap = self.capture_factory(self.cfg.url)
            if not cap or not cap.isOpened():
                self.status.state, self.status.error = "error", "cannot open stream"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 2.0
            self.status.error = ""
            try:
                self._loop(cap)
            except Exception as err:  # noqa: BLE001
                log.exception("camera %s crashed", self.cfg.name)
                self.status.state, self.status.error = "error", str(err)
            finally:
                cap.release()
            self._stop.wait(backoff)
        self.status.state = "stopped"

    def _loop(self, cap) -> None:
        detector = MotionDetector(self.cfg.zones)
        interval = 1.0 / max(0.2, self.cfg.detect_fps)
        next_at = 0.0
        last_ok = time.monotonic()
        fps_count, fps_t0 = 0, time.monotonic()
        while not self._stop.is_set():
            # grab() every frame so the decoder buffer never lags; decode only when needed.
            if not cap.grab():
                if time.monotonic() - last_ok > 10:
                    self.status.error = "stream stalled"
                    return
                time.sleep(0.05)
                continue
            last_ok = time.monotonic()
            if last_ok < next_at:
                continue
            next_at = last_ok + interval
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            with self._latest_lock:
                self._latest_frame, self._latest_frame_at = frame, time.monotonic()
            self.status.state = "online"
            self.status.frames += 1
            self.status.last_frame_at = time.time()
            fps_count += 1
            if last_ok - fps_t0 >= 5:
                self.status.fps = fps_count / (last_ok - fps_t0)
                fps_count, fps_t0 = 0, last_ok
            if self.status.frames % max(1, int(self.cfg.detect_fps)) == 0:
                self._store_snapshot(frame)
            self.process_frame(detector, frame)

    def _store_snapshot(self, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._latest_lock:
                self._latest_jpeg = buf.tobytes()

    def process_frame(self, detector: MotionDetector, frame: np.ndarray) -> list[Detection]:
        hits = detector.process(frame)
        if not hits:
            return []
        now = time.monotonic()
        out: list[Detection] = []
        zones = {z.name: z for z in self.cfg.zones}
        faces_found = False
        if self.cfg.face_mode != "off":
            backend = self.face_backend_getter()
            for hit in hits:
                zone = zones.get(hit.zone, ZoneSpec(hit.zone, []))
                x, y, w, h = zone_bbox(frame.shape[:2], zone)
                region = frame[y:y + h, x:x + w]
                if region.size == 0 or backend is None:
                    continue
                try:
                    faces = backend.detect(region)
                except Exception as err:  # noqa: BLE001
                    self.status.extra["face_error"] = str(err)
                    continue
                for face in faces:
                    faces_found = True
                    emb = backend.embed(region, face) if self.cfg.face_mode != "llm" else None
                    match = (self.gallery.match(emb, self.threshold_getter()) if emb is not None
                             else Match(None, "unknown", 0.0))
                    key = match.person_id if match.known else f"unknown-{hit.zone}"
                    if now - self._last_person_seen.get(key, -1e9) < self.dedupe_seconds:
                        continue
                    self._last_person_seen[key] = now
                    det = Detection(self.cfg, hit.zone,
                                    "face_known" if match.known else "face_unknown",
                                    frame.copy(), crop(region, face.bbox).copy(), match, hit.ratio)
                    out.append(det)
        if not faces_found and now - self._last_motion_event >= self.cfg.cooldown_s:
            self._last_motion_event = now
            best = max(hits, key=lambda h: h.ratio)
            out.append(Detection(self.cfg, best.zone, "motion", frame.copy(),
                                 motion_ratio=best.ratio))
        for det in out:
            self.status.last_event_at = time.time()
            try:
                self.on_detection(det)
            except Exception:  # noqa: BLE001
                log.exception("on_detection failed")
        return out


class MonitorManager:
    def __init__(self, factory: Callable[[CameraConfig], CameraMonitor]):
        self.factory = factory
        self.monitors: dict[int, CameraMonitor] = {}
        self._lock = threading.Lock()

    def sync(self, configs: list[CameraConfig]) -> None:
        wanted = {c.id: c for c in configs}
        with self._lock:
            for cid in list(self.monitors):
                if cid not in wanted or self.monitors[cid].cfg != wanted[cid]:
                    self.monitors.pop(cid).stop()
            for cid, cfg in wanted.items():
                if cid not in self.monitors:
                    mon = self.factory(cfg)
                    self.monitors[cid] = mon
                    mon.start()

    def stop_all(self) -> None:
        with self._lock:
            for mon in self.monitors.values():
                mon.stop()
            self.monitors.clear()

    def get(self, cid: int) -> CameraMonitor | None:
        return self.monitors.get(cid)
