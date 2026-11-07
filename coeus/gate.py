"""Gate opener: recognise an allowed person on one camera and open the gate.

Two ways to start a check:
* webhook: something calls POST /api/hooks/gate/<token> (a doorbell, a Home Assistant
  automation, a motion sensor); Coeus watches the camera for a few seconds.
* continuous: the camera is watched live, and any motion or face on it starts a short check.

Recognition methods:
* local      on-device face match (fast, private)
* llm        send the picture to the LLM; it answers with a name from People and a confidence
* local+llm  on-device first; ask the LLM only when the face is not recognised
* both       on-device match AND the LLM must agree on the same person (strictest)
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import cv2
import numpy as np
from sqlalchemy import select

from coeus import settings_store as ss
from coeus.actions.engine import ActionResult, run_homeassistant, run_webhook
from coeus.db import session_scope
from coeus.models import Camera, GateCheck, Person
from coeus.vision.faces import Match, crop

if TYPE_CHECKING:
    from coeus.services import Runtime

log = logging.getLogger(__name__)

MODES = ("webhook", "continuous", "both")
METHODS = ("local", "llm", "local+llm", "both")
DEFAULT_ACTION = {"type": "homeassistant", "mode": "service", "service": "switch.turn_on",
                  "data": {"entity_id": "switch.main_gate"}}


@dataclass
class GateConfig:
    enabled: bool = False
    camera_id: int | None = None
    mode: str = "webhook"
    person_ids: list[int] = field(default_factory=list)  # empty = everyone in People
    method: str = "local"
    llm_image: str = "face"           # face (crop) | frame (whole picture)
    llm_min_confidence: float = 0.75
    max_llm_calls: int = 3
    window_s: float = 15.0
    interval_s: float = 0.4
    cooldown_s: int = 30
    action: dict = field(default_factory=lambda: dict(DEFAULT_ACTION))
    token: str = ""

    @classmethod
    def load(cls, raw: dict | None) -> GateConfig:
        raw = dict(raw or {})
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        cfg = cls(**known)
        if cfg.mode not in MODES:
            cfg.mode = "webhook"
        if cfg.method not in METHODS:
            cfg.method = "local"
        return cfg

    def dump(self) -> dict:
        return asdict(self)

    @property
    def uses_llm(self) -> bool:
        return self.method in ("llm", "local+llm", "both")


@dataclass
class Verdict:
    outcome: str                  # opened | denied | no_match | no_face | ...
    person_id: int | None = None
    person_name: str = ""
    confidence: float = 0.0
    source: str = ""              # on-device | llm | both
    detail: str = ""


def decide(method: str, allowed: set[int] | None, local: Match | None,
           llm: tuple[int | None, str, float] | None, llm_min: float) -> Verdict | None:
    """Decide for one face. Returns None when undecided (keep looking).

    allowed=None means everyone in People. llm is (person_id, name, confidence) or None.
    """
    def ok(pid):
        return pid is not None and (allowed is None or pid in allowed)

    llm_known = llm is not None and llm[0] is not None and llm[2] >= llm_min
    if method == "local":
        if local and local.known:
            return Verdict("opened" if ok(local.person_id) else "denied", local.person_id,
                           local.name, local.score, "on-device")
        return None
    if method == "llm":
        if llm_known:
            return Verdict("opened" if ok(llm[0]) else "denied", llm[0], llm[1], llm[2], "llm")
        return None
    if method == "local+llm":
        if local and local.known:
            return Verdict("opened" if ok(local.person_id) else "denied", local.person_id,
                           local.name, local.score, "on-device")
        if llm_known:
            return Verdict("opened" if ok(llm[0]) else "denied", llm[0], llm[1], llm[2], "llm")
        return None
    if method == "both":
        if not (local and local.known):
            return None
        if llm is None:
            return None
        if llm_known and llm[0] == local.person_id:
            return Verdict("opened" if ok(local.person_id) else "denied", local.person_id,
                           local.name, min(local.score, llm[2]), "both")
        return Verdict("no_match", local.person_id, local.name, local.score, "both",
                       f"on-device says {local.name}, LLM says "
                       f"{llm[1] or 'unknown'} ({llm[2]:.2f})")
    return None


class GateController:
    def __init__(self, runtime: Runtime):
        self.rt = runtime
        self._busy = threading.Lock()
        self._last_open = 0.0
        self._last_auto = 0.0

    # -- config ----------------------------------------------------------------
    def config(self) -> GateConfig:
        with session_scope() as db:
            cfg = GateConfig.load(ss.get(db, "gate"))
            if not cfg.token:
                cfg.token = secrets.token_urlsafe(24)
                ss.set_(db, "gate", cfg.dump())
        return cfg

    def save(self, cfg: GateConfig) -> None:
        with session_scope() as db:
            ss.set_(db, "gate", cfg.dump())

    # -- frames ----------------------------------------------------------------
    def _frame_source(self, cam_id: int):
        """A callable returning the next frame (or None), plus a close() function."""
        mon = self.rt.monitors.get(cam_id)
        if mon is not None and mon.status.state == "online":
            last = [0.0]

            def from_monitor():
                frame, at = mon.latest_frame()
                if frame is None or at == last[0]:
                    return None
                last[0] = at
                return frame
            return from_monitor, lambda: None
        with session_scope() as db:
            cam = db.get(Camera, cam_id)
            url = (cam.rtsp_url or cam.sub_rtsp_url) if cam else ""
        if not url:
            raise RuntimeError("the gate camera has no stream URL")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError("cannot open the gate camera stream")

        def from_capture():
            ok, frame = cap.read()
            return frame if ok else None
        return from_capture, cap.release

    # -- LLM -------------------------------------------------------------------
    def _llm_identify(self, image: np.ndarray) -> tuple[int | None, str, float, str]:
        provider = self.rt.llm()
        if provider is None:
            raise RuntimeError("no LLM provider is set up (Settings → LLM provider)")
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        ident = provider.identify(buf.tobytes(), self.rt.reference_images())
        pid = None
        if ident.name:
            with session_scope() as db:
                pid = db.scalar(select(Person.id).where(Person.name == ident.name))
        return pid, ident.name or "", float(ident.confidence), ident.reason

    # -- the check -------------------------------------------------------------
    def evaluate_image(self, image: np.ndarray, cfg: GateConfig, allowed: set[int] | None,
                       budget: list[int], force_llm: bool = False) -> tuple[Verdict | None, int]:
        """Look at one picture. Returns (verdict or None, faces found)."""
        backend = self.rt.face_backend()
        faces = backend.detect(image) if cfg.method != "llm" or cfg.llm_image == "face" else []
        faces.sort(key=lambda f: f.bbox[2] * f.bbox[3], reverse=True)
        threshold = float(self.rt._config().get("faces.threshold") or 0.363)
        pending: Verdict | None = None
        for face in faces[:3]:
            local = None
            if cfg.method != "llm":
                local = self.rt.gallery.match(backend.embed(image, face), threshold)
            want_llm = cfg.uses_llm and (
                cfg.method == "llm" or (cfg.method == "local+llm" and not (local and local.known))
                or (cfg.method == "both" and local is not None and local.known))
            llm = None
            if want_llm and (budget[0] > 0 or force_llm):
                budget[0] -= 1
                pic = crop(image, face.bbox, pad=0.4) if cfg.llm_image == "face" else image
                pid, name, conf, reason = self._llm_identify(pic)
                llm = (pid, name, conf)
            v = decide(cfg.method, allowed, local, llm, cfg.llm_min_confidence)
            if v and v.outcome == "opened":
                return v, len(faces)
            pending = pending or v
        if not faces and cfg.method == "llm" and cfg.llm_image == "frame" and \
                (budget[0] > 0 or force_llm):
            budget[0] -= 1
            pid, name, conf, reason = self._llm_identify(image)
            v = decide("llm", allowed, None, (pid, name, conf), cfg.llm_min_confidence)
            if v:
                v.detail = reason
                return v, 0
        return pending, len(faces)

    def check(self, trigger: str, window_s: float | None = None) -> dict:
        """Watch the gate camera until an allowed person is seen or the window ends."""
        cfg = self.config()
        if not cfg.enabled or cfg.camera_id is None:
            return {"outcome": "disabled", "detail": "The gate is not set up or is switched off."}
        if not self._busy.acquire(blocking=False):
            return {"outcome": "busy", "detail": "A check is already running."}
        t0 = time.monotonic()
        verdict, snapshot, faces_seen, budget = None, None, 0, [max(0, cfg.max_llm_calls)]
        allowed = set(cfg.person_ids) or None
        try:
            if time.monotonic() - self._last_open < cfg.cooldown_s:
                return self._record(trigger, cfg, Verdict("cooldown", detail="The gate was "
                                    "opened moments ago."), None, t0, budget)
            next_frame, close = self._frame_source(cfg.camera_id)
            try:
                deadline = t0 + (window_s or cfg.window_s)
                while time.monotonic() < deadline:
                    frame = next_frame()
                    if frame is None:
                        time.sleep(0.05)
                        continue
                    snapshot = frame
                    v, n = self.evaluate_image(frame, cfg, allowed, budget)
                    faces_seen += n
                    if v:
                        verdict = v
                        if v.outcome == "opened":
                            snapshot = frame
                            break
                    time.sleep(cfg.interval_s)
            finally:
                close()
            if verdict is None:
                verdict = Verdict("no_face" if not faces_seen else "no_match",
                                  detail="Nobody recognised." if faces_seen
                                  else "No face seen in the time window.")
            return self._record(trigger, cfg, verdict, snapshot, t0, budget)
        except Exception as err:  # noqa: BLE001
            log.exception("gate check failed")
            return self._record(trigger, cfg, Verdict("error", detail=str(err)), snapshot, t0,
                                budget)
        finally:
            self._busy.release()

    def on_live_activity(self, camera_id: int) -> None:
        """Called from the live monitor; starts a short check in continuous mode."""
        cfg = self.config()
        if not cfg.enabled or cfg.mode not in ("continuous", "both") or camera_id != cfg.camera_id:
            return
        now = time.monotonic()
        if now - self._last_auto < 3 or now - self._last_open < cfg.cooldown_s:
            return
        self._last_auto = now
        threading.Thread(target=self.check, args=("continuous", min(cfg.window_s, 6.0)),
                         daemon=True).start()

    def open_gate(self, cfg: GateConfig, person: str, confidence: float) -> ActionResult:
        import httpx
        now = datetime.now(self.rt.tz)
        vars_ = {"person": person, "confidence": f"{confidence:.2f}", "camera": "gate",
                 "zone": "gate", "event_type": "gate_open", "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                 "iso_time": now.isoformat(), "event_id": "", "description": "",
                 "snapshot_url": ""}
        conf = self.rt._config()
        with httpx.Client(timeout=10) as client:
            if cfg.action.get("type") == "webhook":
                return run_webhook(cfg.action, vars_, client)
            return run_homeassistant(cfg.action, vars_, client, conf.get("ha.url", ""),
                                     conf.get("ha.token", ""))

    def _record(self, trigger: str, cfg: GateConfig, v: Verdict, frame, t0: float,
                budget: list[int]) -> dict:
        action_ok = None
        if v.outcome == "opened":
            res = self.open_gate(cfg, v.person_name, v.confidence)
            action_ok = res.ok
            if res.ok:
                self._last_open = time.monotonic()
            v.detail = (v.detail + " " if v.detail else "") + f"Gate action: {res.detail[:200]}"
        snap = ""
        if frame is not None:
            d = self.rt.settings.media_dir / "gate"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S%f}.jpg"
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            snap = str(path)
        with session_scope() as db:
            row = GateCheck(trigger=trigger, outcome=v.outcome, method=cfg.method,
                            person_id=v.person_id, person_name=v.person_name,
                            confidence=v.confidence, source=v.source, snapshot_path=snap,
                            action_ok=action_ok, detail=v.detail[:2000],
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            llm_calls=max(0, cfg.max_llm_calls) - budget[0])
            db.add(row)
            db.flush()
            check_id = row.id
        return {"check_id": check_id, "outcome": v.outcome, "opened": v.outcome == "opened"
                and bool(action_ok), "person": v.person_name or None,
                "confidence": round(v.confidence, 3), "source": v.source, "detail": v.detail,
                "duration_ms": int((time.monotonic() - t0) * 1000)}
