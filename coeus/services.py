"""Application runtime: wires the DB to the face gallery, monitors, actions and LLMs."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
from sqlalchemy import select

from coeus import settings_store as ss
from coeus.actions.engine import ActionEngine, ActionResult, EventContext, RuleView
from coeus.config import Settings
from coeus.db import session_scope
from coeus.llm.providers import LLMProvider, get_provider
from coeus.models import ActionLog, Camera, Event, FaceSample, Person, Rule
from coeus.vision.faces import Gallery, Match, SFaceBackend
from coeus.vision.monitor import CameraConfig, CameraMonitor, Detection, MonitorManager
from coeus.vision.motion import ZoneSpec

log = logging.getLogger(__name__)


def rule_view(r: Rule) -> RuleView:
    return RuleView(id=r.id, name=r.name, enabled=r.enabled, event_types=r.event_types or [],
                    camera_ids=[int(x) for x in r.camera_ids or []],
                    zone_names=r.zone_names or [],
                    person_ids=[int(x) for x in r.person_ids or []],
                    time_start=r.time_start or "", time_end=r.time_end or "",
                    days=[int(x) for x in r.days or []], cooldown_s=r.cooldown_s or 0,
                    actions=r.actions or [], last_fired_at=r.last_fired_at)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.gallery = Gallery()
        self._face_backend: SFaceBackend | None = None
        self._face_lock = threading.Lock()
        self.monitors = MonitorManager(self._make_monitor)
        self.actions = ActionEngine(
            load_rules=self._load_rules, on_fired=self._rule_fired,
            log_result=self._log_action, get_config=self._config, describe=self._describe)
        self._cfg_cache: tuple[float, dict] = (0.0, {})

    # -- config ------------------------------------------------------------------
    def _config(self) -> dict:
        ts, cfg = self._cfg_cache
        if time.monotonic() - ts < 5:
            return cfg
        with session_scope() as db:
            cfg = {k: ss.get(db, k) for k in ss.DEFAULTS}
        self._cfg_cache = (time.monotonic(), cfg)
        return cfg

    def invalidate_config(self) -> None:
        self._cfg_cache = (0.0, {})

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    # -- faces -------------------------------------------------------------------
    def face_backend(self) -> SFaceBackend:
        with self._face_lock:
            if self._face_backend is None:
                self._face_backend = SFaceBackend(self.settings.models_dir)
            return self._face_backend

    def reload_gallery(self) -> None:
        with session_scope() as db:
            rows = db.execute(select(Person.id, Person.name, FaceSample.embedding)
                              .join(FaceSample, FaceSample.person_id == Person.id)).all()
        self.gallery.load([(r[0], r[1], r[2]) for r in rows])
        log.info("face gallery loaded: %d samples", len(self.gallery))

    def reference_images(self, per_person: int = 2, max_people: int = 12) -> list[tuple[str, bytes]]:
        refs: list[tuple[str, bytes]] = []
        with session_scope() as db:
            for person in db.scalars(select(Person).order_by(Person.name).limit(max_people)):
                for sample in person.samples[:per_person]:
                    p = Path(sample.image_path)
                    if p.exists():
                        refs.append((person.name, p.read_bytes()))
        return refs

    # -- LLM ---------------------------------------------------------------------
    def llm(self) -> LLMProvider | None:
        cfg = self._config()
        try:
            return get_provider(cfg.get("llm.provider") or "none", cfg.get("llm.model") or "",
                                cfg.get("llm.api_key") or "", cfg.get("llm.base_url") or "",
                                int(cfg.get("llm.max_calls_per_hour") or 60))
        except Exception:  # noqa: BLE001
            log.exception("could not create LLM provider")
            return None

    def _describe(self, ev: EventContext) -> str:
        provider = self.llm()
        if provider is None:
            raise RuntimeError("no LLM provider configured")
        data = Path(ev.snapshot_path).read_bytes()
        text = provider.describe(data, ev.camera, ev.zone)
        with session_scope() as db:
            row = db.get(Event, ev.event_id)
            if row:
                row.description = text
        return text

    # -- monitors ----------------------------------------------------------------
    def _make_monitor(self, cfg: CameraConfig) -> CameraMonitor:
        return CameraMonitor(cfg, self.gallery, self.face_backend, self.handle_detection,
                             lambda: float(self._config().get("faces.threshold") or 0.363),
                             float(self._config().get("faces.dedupe_seconds") or 60))

    def camera_configs(self) -> list[CameraConfig]:
        out = []
        with session_scope() as db:
            for cam in db.scalars(select(Camera).where(Camera.monitor_enabled.is_(True))):
                url = cam.sub_rtsp_url or cam.rtsp_url
                if not url:
                    continue
                zones = [ZoneSpec(z.name, z.points or [], z.kind, z.min_motion_ratio)
                         for z in cam.zones]
                out.append(CameraConfig(cam.id, cam.name, url, zones, cam.detect_fps,
                                        cam.face_mode, cam.cooldown_s))
        return out

    def sync_monitors(self) -> None:
        self.monitors.sync(self.camera_configs())

    # -- events ------------------------------------------------------------------
    def handle_detection(self, det: Detection) -> None:
        match = det.match or Match(None, "unknown", 0.0)
        event_type = det.event_type
        description = ""
        mode = det.camera.face_mode
        if det.face_crop is not None and not match.known and mode in ("llm", "local+llm"):
            provider = self.llm()
            if provider is not None:
                try:
                    ok, buf = cv2.imencode(".jpg", det.face_crop)
                    ident = provider.identify(buf.tobytes(), self.reference_images())
                    if ident.name:
                        with session_scope() as db:
                            pid = db.scalar(select(Person.id).where(Person.name == ident.name))
                        match = Match(pid, ident.name, ident.confidence)
                        event_type = "face_known"
                    description = ident.reason
                except Exception as err:  # noqa: BLE001
                    log.warning("LLM identify failed: %s", err)
        self.record_event(det.camera.id, det.camera.name, det.zone, event_type, match,
                          det.frame, det.face_crop, description,
                          {"motion_ratio": round(det.motion_ratio, 4)})

    def record_event(self, camera_id, camera_name, zone, event_type, match: Match, frame,
                     face_crop=None, description: str = "", extra: dict | None = None) -> int:
        now = datetime.now(timezone.utc)
        day_dir = self.settings.media_dir / "events" / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%H%M%S%f")
        snap = day_dir / f"{camera_id}-{stamp}.jpg"
        cv2.imwrite(str(snap), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        face_path = ""
        if face_crop is not None and face_crop.size:
            fp = day_dir / f"{camera_id}-{stamp}-face.jpg"
            cv2.imwrite(str(fp), face_crop)
            face_path = str(fp)
        with session_scope() as db:
            ev = Event(ts=now, camera_id=camera_id, camera_name=camera_name, zone=zone,
                       type=event_type, person_id=match.person_id,
                       person_name=match.name if match.known else "",
                       confidence=match.score, snapshot_path=str(snap), face_path=face_path,
                       description=description, extra=extra or {})
            db.add(ev)
            db.flush()
            event_id = ev.id
        self.actions.submit(self.event_context(event_id))
        return event_id

    def event_context(self, event_id: int) -> EventContext:
        with session_scope() as db:
            ev = db.get(Event, event_id)
            assert ev is not None
            base = self.settings.public_url.rstrip("/")
            ts = ev.ts if ev.ts.tzinfo else ev.ts.replace(tzinfo=timezone.utc)
            return EventContext(
                event_id=ev.id, event_type=ev.type, camera_id=ev.camera_id,
                camera=ev.camera_name, zone=ev.zone, person_id=ev.person_id,
                person=ev.person_name or "unknown", confidence=ev.confidence,
                time=ts.astimezone(self.tz),
                snapshot_url=f"{base}/media/events/{ev.id}/snapshot" if base else "",
                snapshot_path=ev.snapshot_path, description=ev.description, extra=ev.extra or {})

    def _load_rules(self) -> list[RuleView]:
        with session_scope() as db:
            return [rule_view(r) for r in db.scalars(select(Rule).where(Rule.enabled.is_(True)))]

    def _rule_fired(self, rule_id: int, when: datetime) -> None:
        with session_scope() as db:
            r = db.get(Rule, rule_id)
            if r:
                r.last_fired_at = when

    def _log_action(self, event_id: int, rule_id: int, atype: str, result: ActionResult) -> None:
        with session_scope() as db:
            db.add(ActionLog(event_id=event_id, rule_id=rule_id, action_type=atype,
                             ok=result.ok, detail=result.detail[:2000]))

    def shutdown(self) -> None:
        self.monitors.stop_all()
        self.actions.pool.shutdown(wait=False, cancel_futures=True)
