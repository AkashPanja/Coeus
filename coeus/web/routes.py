"""HTML pages and form handlers."""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coeus import settings_store as ss
from coeus.actions.engine import ACTION_TYPES, EVENT_TYPES
from coeus.db import get_db
from coeus.jobs import archive
from coeus.models import (ActionLog, ArchiveJob, Camera, Event, FaceSample, Person, Rule, User,
                          YouTubeAccount, Zone)
from coeus.recordings.synology import list_camera_folders, summarize_days
from coeus.security import (admin_user, authenticate, current_user, hash_password, throttle,
                            verify_password)
from coeus.services import rule_view
from coeus.vision import faces as fr
from coeus.web.common import flash, redirect, render
from coeus.youtube import uploader as yt

router = APIRouter()


def rt(request: Request):
    return request.app.state.runtime


def settings_of(request: Request):
    return request.app.state.settings


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


def lib_root(request: Request, db: Session) -> Path:
    return archive.recordings_root(db, settings_of(request))


def accounts_of(db: Session) -> list[YouTubeAccount]:
    return list(db.scalars(select(YouTubeAccount).order_by(YouTubeAccount.name)))


def _bg(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


# --- Auth ---------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), user=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          next: str = Form("/"), db: Session = Depends(get_db, scope="function")):
    ip = request.client.host if request.client else "?"
    if throttle.is_locked(ip, username):
        return render(request, "login.html", next=next, user=None,
                      error="Too many failed attempts. Try again in a few minutes.")
    user = authenticate(db, username, password)
    if user is None:
        throttle.fail(ip, username)
        return render(request, "login.html", next=next, user=None,
                      error="Invalid username or password.")
    throttle.reset(ip, username)
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update({"uid": user.id, "csrf": csrf})
    return redirect(_safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# --- Dashboard ----------------------------------------------------------------

@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    runtime = rt(request)
    cams = db.scalars(select(Camera).order_by(Camera.name)).all()
    statuses = {c.id: (runtime.monitors.get(c.id).status if runtime.monitors.get(c.id) else None)
                for c in cams}
    events = db.scalars(select(Event).order_by(Event.ts.desc()).limit(12)).all()
    job_counts = dict(db.execute(select(ArchiveJob.status, func.count())
                                 .group_by(ArchiveJob.status)).all())
    accounts = accounts_of(db)
    quotas = {a.id: archive.quota_state(db, a) for a in accounts}
    s = settings_of(request)
    disk = None
    try:
        disk = shutil.disk_usage(s.data_dir)
    except OSError:
        pass
    from coeus.models import GateCheck
    gate_cfg = runtime.gate.config()
    live = next((c for c in cams if c.monitor_enabled), None)
    return render(request, "dashboard.html", cams=cams, statuses=statuses, events=events,
                  gate_cfg=gate_cfg, live=live,
                  gate_cam=db.get(Camera, gate_cfg.camera_id) if gate_cfg.camera_id else None,
                  gate_checks=db.scalars(select(GateCheck).order_by(GateCheck.ts.desc())
                                         .limit(6)).all(),
                  job_counts=job_counts, accounts=accounts, quotas=quotas, disk=disk,
                  people=db.scalar(select(func.count()).select_from(Person)))


# --- Cameras ------------------------------------------------------------------

CAMERA_FIELDS = ("name", "rtsp_url", "sub_rtsp_url", "onvif_host", "onvif_user",
                 "recording_folder", "yt_title_template", "face_mode")


@router.get("/cameras")
def cameras(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    cams = db.scalars(select(Camera).order_by(Camera.name)).all()
    runtime = rt(request)
    statuses = {c.id: (runtime.monitors.get(c.id).status if runtime.monitors.get(c.id) else None)
                for c in cams}
    return render(request, "cameras.html", cams=cams, statuses=statuses)


@router.get("/cameras/new")
@router.get("/cameras/{cam_id}/edit")
def camera_form(request: Request, cam_id: int | None = None, folder: str = "",
                user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    cam = db.get(Camera, cam_id) if cam_id else Camera(name=folder, recording_folder=folder,
                                                       yt_title_template="{camera} — {date} "
                                                       "(part {part}/{parts})",
                                                       yt_privacy="private", face_mode="local",
                                                       detect_fps=5.0, cooldown_s=30,
                                                       onvif_port=80)
    if cam is None:
        raise HTTPException(404)
    folders = list_camera_folders(lib_root(request, db))
    return render(request, "camera_form.html", cam=cam, folders=folders,
                  accounts=accounts_of(db))


@router.post("/cameras/save")
async def camera_save(request: Request, user: User = Depends(admin_user),
                      db: Session = Depends(get_db, scope="function")):
    form = await request.form()
    cam_id = form.get("id")
    cam = db.get(Camera, int(cam_id)) if cam_id else Camera()
    if cam is None:
        raise HTTPException(404)
    for f in CAMERA_FIELDS:
        setattr(cam, f, str(form.get(f, "")).strip())
    if form.get("onvif_password"):
        cam.onvif_password = str(form.get("onvif_password"))
    cam.onvif_port = int(form.get("onvif_port") or 80)
    cam.detect_fps = max(0.5, min(15.0, float(form.get("detect_fps") or 5)))
    cam.cooldown_s = max(0, int(form.get("cooldown_s") or 30))
    cam.monitor_enabled = form.get("monitor_enabled") == "on"
    cam.archive_enabled = form.get("archive_enabled") == "on"
    acct_id = str(form.get("yt_account_id") or "")
    cam.yt_account_id = int(acct_id) if acct_id.isdigit() and db.get(YouTubeAccount, int(acct_id)) \
        else None
    if not cam.name:
        flash(request, "Name is required", "error")
        return redirect("/cameras/new")
    cam.yt_privacy = "private"  # archives are always private
    if not cam_id:
        db.add(cam)
    switched = ""
    if cam.monitor_enabled:
        db.flush()
        others = [c for c in db.scalars(select(Camera).where(Camera.monitor_enabled.is_(True),
                                                             Camera.id != cam.id))]
        limit = max(1, int(ss.get(db, "live.max_cameras") or 1))
        while len(others) >= limit:
            off = others.pop(0)
            off.monitor_enabled = False
            switched += f" Live watching moved from {off.name} to {cam.name} (limit: {limit})."
    problem, next_url = "", "/cameras"
    if cam.archive_enabled and not cam.recording_folder:
        cam.archive_enabled, problem = False, "Choose a recording folder to archive this camera."
    elif cam.archive_enabled and cam.yt_account_id is None:
        cam.archive_enabled = False
        if not accounts_of(db):
            problem = ("Saved, but archiving stays off: there is no YouTube account yet. "
                       "Add one now; you'll come back to this camera afterwards.")
        else:
            problem = "Saved, but archiving stays off: choose which YouTube account to upload to."
    db.flush()
    if problem:
        next_url = (f"/youtube/new?next=/cameras/{cam.id}/edit" if not accounts_of(db)
                    and "no YouTube account" in problem else f"/cameras/{cam.id}/edit")
    db.commit()
    rt(request).sync_monitors()
    flash(request, (problem or f"Saved camera {cam.name}.") + switched,
          "error" if problem else "ok")
    return redirect(next_url)


@router.post("/cameras/{cam_id}/delete")
def camera_delete(request: Request, cam_id: int, user: User = Depends(admin_user),
                  db: Session = Depends(get_db, scope="function")):
    cam = db.get(Camera, cam_id)
    if cam:
        db.delete(cam)
        db.flush()
        rt(request).sync_monitors()
        flash(request, f"Deleted camera {cam.name}")
    return redirect("/cameras")


@router.post("/cameras/onvif-probe")
def onvif_probe(host: str = Form(...), port: int = Form(80), username: str = Form(""),
                password: str = Form(""), user: User = Depends(admin_user)):
    from coeus.vision.onvif import get_stream_uris
    try:
        return JSONResponse({"ok": True, "profiles": get_stream_uris(host, port, username,
                                                                     password)})
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(err)}, status_code=200)


@router.post("/cameras/test-stream")
def camera_test_stream(url: str = Form(...), user: User = Depends(admin_user)):
    import base64
    import cv2
    if not url.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return JSONResponse({"ok": False, "error": "URL must start with rtsp:// or http(s)://"})
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return JSONResponse({"ok": False, "error": "cannot open stream (address, login "
                                                       "or network)"})
        for _ in range(15):
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (320, int(320 * h / w))) if w > 320 else frame
                _, buf = cv2.imencode(".jpg", small)
                return JSONResponse({"ok": True, "width": w, "height": h,
                                     "fps": round(cap.get(cv2.CAP_PROP_FPS) or 0, 1),
                                     "preview": "data:image/jpeg;base64,"
                                                + base64.b64encode(buf.tobytes()).decode()})
        return JSONResponse({"ok": False, "error": "connected but no frames were decoded"})
    finally:
        cap.release()


@router.get("/cameras/{cam_id}/snapshot.jpg")
def camera_snapshot(request: Request, cam_id: int, user: User = Depends(current_user),
                    db: Session = Depends(get_db, scope="function")):
    mon = rt(request).monitors.get(cam_id)
    data = mon.latest_jpeg() if mon else None
    if data is None:
        cam = db.get(Camera, cam_id)
        url = (cam.rtsp_url or cam.sub_rtsp_url) if cam else ""
        if url:
            data = _grab_one(url)
    if data is None:
        raise HTTPException(404, "No snapshot available")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _grab_one(url: str) -> bytes | None:
    import cv2
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        for _ in range(10):
            ok, frame = cap.read()
            if ok and frame is not None:
                ok, buf = cv2.imencode(".jpg", frame)
                return buf.tobytes() if ok else None
    finally:
        cap.release()
    return None


@router.get("/cameras/{cam_id}/zones")
def zones_page(request: Request, cam_id: int, user: User = Depends(admin_user),
               db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    cam = db.get(Camera, cam_id)
    if cam is None:
        raise HTTPException(404)
    zones = [{"name": z.name, "kind": z.kind, "points": z.points,
              "min_motion_ratio": z.min_motion_ratio} for z in cam.zones]
    return render(request, "zones.html", cam=cam, zones_json=json.dumps(zones))


@router.post("/cameras/{cam_id}/zones")
async def zones_save(request: Request, cam_id: int, user: User = Depends(admin_user),
                     db: Session = Depends(get_db, scope="function")):
    cam = db.get(Camera, cam_id)
    if cam is None:
        raise HTTPException(404)
    payload = await request.json()
    cam.zones.clear()
    db.flush()
    seen = set()
    for z in payload.get("zones", []):
        name = str(z.get("name", "")).strip()[:64] or f"zone{len(seen) + 1}"
        if name in seen:
            continue
        seen.add(name)
        pts = [[min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))]
               for x, y in z.get("points", [])][:64]
        if len(pts) < 3:
            continue
        cam.zones.append(Zone(name=name, kind="exclude" if z.get("kind") == "exclude"
                              else "include", points=pts,
                              min_motion_ratio=max(0.0005, min(1.0, float(
                                  z.get("min_motion_ratio", 0.01))))))
    db.flush()
    rt(request).sync_monitors()
    return JSONResponse({"ok": True, "count": len(cam.zones)})


# --- Recordings & archive jobs ----------------------------------------------------

@router.get("/recordings")
def recordings(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    root = lib_root(request, db)
    folders = list_camera_folders(root)
    linked = {c.recording_folder: c for c in db.scalars(select(Camera))
              if c.recording_folder}
    return render(request, "recordings.html", folders=folders, linked=linked, root=root,
                  root_rel=ss.get(db, "library.root") or "", accounts=accounts_of(db))


@router.post("/recordings/{folder}/add")
def recording_add_camera(request: Request, folder: str, yt_account_id: str = Form(""),
                         user: User = Depends(admin_user),
                         db: Session = Depends(get_db, scope="function")):
    if folder not in list_camera_folders(lib_root(request, db)):
        raise HTTPException(404)
    if db.scalar(select(Camera).where(Camera.recording_folder == folder)):
        flash(request, f"“{folder}” already has a camera", "error")
        return redirect("/recordings")
    acct = db.get(YouTubeAccount, int(yt_account_id)) if yt_account_id.isdigit() else None
    if acct is None:
        flash(request, f"Choose which YouTube account “{folder}” should upload to"
              + ("" if accounts_of(db) else " — add one first") + ".", "error")
        return redirect("/recordings" if accounts_of(db) else "/youtube/new?next=/recordings")
    name = folder
    if db.scalar(select(Camera).where(Camera.name == name)):
        name = f"{folder} (recordings)"
    db.add(Camera(name=name, recording_folder=folder, archive_enabled=True,
                  yt_account_id=acct.id))
    flash(request, f"Added “{folder}”: archived daily to {acct.name}"
          + ("" if acct.connected else " (account not connected yet!)"))
    return redirect("/recordings")


@router.get("/recordings/{folder}")
def recording_days(request: Request, folder: str, user: User = Depends(admin_user),
                   db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    root = lib_root(request, db)
    if folder not in list_camera_folders(root):
        raise HTTPException(404)
    days = summarize_days(root, folder,
                          settle_seconds=float(ss.get(db, "archive.settle_minutes")) * 60)
    jobs = db.scalars(select(ArchiveJob).where(ArchiveJob.camera_folder == folder)).all()
    by_day: dict[str, list[ArchiveJob]] = {}
    for j in jobs:
        by_day.setdefault(j.day, []).append(j)
    return render(request, "recording_days.html", folder=folder, days=days, jobs=by_day)


@router.post("/recordings/{folder}/{day}/archive")
def archive_day(request: Request, folder: str, day: str, replan: str = Form(""),
                user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    s = settings_of(request)
    if folder not in list_camera_folders(lib_root(request, db)):
        raise HTTPException(404)
    cam = db.scalar(select(Camera).where(Camera.recording_folder == folder))
    if cam is None or cam.yt_account_id is None:
        flash(request, f"Choose a YouTube account for “{folder}” first", "error")
        return redirect("/recordings")
    try:
        d = date.fromisoformat(day)
        jobs = archive.plan_day(db, s, folder, d, replace=bool(replan))
    except ValueError as err:
        flash(request, str(err), "error")
        return redirect(f"/recordings/{folder}")
    db.commit()
    _bg(archive.run_worker, s)
    flash(request, f"Queued {len(jobs)} part(s) for {folder} {day}")
    return redirect("/jobs")


@router.get("/jobs")
def jobs_page(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    jobs = db.scalars(select(ArchiveJob).order_by(ArchiveJob.day.desc(), ArchiveJob.camera_folder,
                                                  ArchiveJob.part).limit(300)).all()
    accounts = accounts_of(db)
    return render(request, "jobs.html", jobs=jobs, accounts=accounts,
                  quotas={a.id: archive.quota_state(db, a) for a in accounts})


@router.post("/jobs/{job_id}/retry")
def job_retry(request: Request, job_id: int, user: User = Depends(admin_user),
              db: Session = Depends(get_db, scope="function")):
    job = db.get(ArchiveJob, job_id)
    if job and job.status in ("failed", "cancelled", "merged"):
        merged_ok = job.output_path and Path(job.output_path).exists()
        job.status = "merged" if merged_ok else "pending"
        job.error = ""
        db.commit()
        _bg(archive.run_worker, settings_of(request))
    return redirect("/jobs")


@router.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: int, user: User = Depends(admin_user),
               db: Session = Depends(get_db, scope="function")):
    job = db.get(ArchiveJob, job_id)
    if job and job.status in ("pending", "merged", "failed"):
        job.status = "cancelled"
    return redirect("/jobs")


@router.post("/jobs/run")
def jobs_run(request: Request, user: User = Depends(admin_user)):
    _bg(archive.run_worker, settings_of(request))
    flash(request, "Worker started")
    return redirect("/jobs")


# --- People -------------------------------------------------------------------

@router.get("/persons")
def persons(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    people = db.scalars(select(Person).order_by(Person.name)).all()
    return render(request, "persons.html", people=people)


@router.post("/persons")
def person_create(request: Request, name: str = Form(...), notes: str = Form(""),
                  user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    name = name.strip()
    if not name or db.scalar(select(Person).where(Person.name == name)):
        flash(request, "Name is empty or already exists", "error")
        return redirect("/persons")
    p = Person(name=name, notes=notes)
    db.add(p)
    db.flush()
    return redirect(f"/persons/{p.id}")


@router.get("/persons/{pid}")
def person_page(request: Request, pid: int, user: User = Depends(admin_user),
                db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    p = db.get(Person, pid)
    if p is None:
        raise HTTPException(404)
    return render(request, "person.html", person=p)


@router.post("/persons/{pid}/samples")
async def person_add_samples(request: Request, pid: int, files: list[UploadFile] = File(...),
                             user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    p = db.get(Person, pid)
    if p is None:
        raise HTTPException(404)
    runtime = rt(request)
    backend = runtime.face_backend()
    out_dir = settings_of(request).media_dir / "faces" / str(pid)
    out_dir.mkdir(parents=True, exist_ok=True)
    added, errors = 0, []
    for f in files:
        data = await f.read()
        if len(data) > 15 * 1024 * 1024:
            errors.append(f"{f.filename}: larger than 15 MB")
            continue
        img = fr.decode_image(data)
        if img is None:
            errors.append(f"{f.filename}: not an image")
            continue
        try:
            emb = fr.enroll_embedding(backend, img)
            faces = backend.detect(img)
            faces.sort(key=lambda x: x.bbox[2] * x.bbox[3], reverse=True)
        except Exception as err:  # noqa: BLE001
            errors.append(f"{f.filename}: {err}")
            continue
        import cv2
        path = out_dir / f"{uuid.uuid4().hex}.jpg"
        cv2.imwrite(str(path), fr.crop(img, faces[0].bbox, pad=0.35))
        db.add(FaceSample(person_id=pid, image_path=str(path), backend=backend.name,
                          embedding=emb.astype("float32").tobytes()))
        added += 1
    db.commit()
    runtime.reload_gallery()
    msg = f"Added {added} face sample(s)."
    if errors:
        msg += " Problems: " + "; ".join(errors)
    flash(request, msg, "error" if errors and not added else "ok")
    return redirect(f"/persons/{pid}")


@router.post("/persons/{pid}/samples/{sid}/delete")
def sample_delete(request: Request, pid: int, sid: int, user: User = Depends(admin_user),
                  db: Session = Depends(get_db, scope="function")):
    s = db.get(FaceSample, sid)
    if s and s.person_id == pid:
        Path(s.image_path).unlink(missing_ok=True)
        db.delete(s)
        db.commit()
        rt(request).reload_gallery()
    return redirect(f"/persons/{pid}")


@router.post("/persons/{pid}/delete")
def person_delete(request: Request, pid: int, user: User = Depends(admin_user),
                  db: Session = Depends(get_db, scope="function")):
    p = db.get(Person, pid)
    if p:
        for s in p.samples:
            Path(s.image_path).unlink(missing_ok=True)
        db.delete(p)
        db.commit()
        rt(request).reload_gallery()
        flash(request, f"Deleted {p.name} and their face samples")
    return redirect("/persons")


@router.get("/media/faces/{sid}")
def face_image(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    s = db.get(FaceSample, sid)
    if s is None or not Path(s.image_path).exists():
        raise HTTPException(404)
    return FileResponse(s.image_path, media_type="image/jpeg")


# --- Rules --------------------------------------------------------------------

ACTION_EXAMPLES = {
    "webhook": {"type": "webhook", "method": "POST", "url": "http://192.168.0.111:5678/webhook/coeus",
                "headers": {"Content-Type": "application/json"},
                "body": {"event": "{event_type}", "person": "{person}", "camera": "{camera}",
                         "zone": "{zone}", "time": "{time}", "snapshot": "{snapshot_url}"}},
    "homeassistant": {"type": "homeassistant", "mode": "service",
                      "service": "notify.mobile_app_phone",
                      "data": {"title": "{camera}", "message": "{person} at {zone} ({time})"}},
    "homeassistant_event": {"type": "homeassistant", "mode": "event", "event_type": "coeus_event"},
    "llm_describe": {"type": "llm_describe"},
}


@router.get("/rules")
def rules(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    rows = db.scalars(select(Rule).order_by(Rule.name)).all()
    cams = {c.id: c.name for c in db.scalars(select(Camera))}
    people = {p.id: p.name for p in db.scalars(select(Person))}
    return render(request, "rules.html", rules=rows, cams=cams, people=people)


@router.get("/rules/new")
@router.get("/rules/{rid}/edit")
def rule_form(request: Request, rid: int | None = None, user: User = Depends(admin_user),
              db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    rule = db.get(Rule, rid) if rid else Rule(name="", enabled=True, event_types=["face_known"],
                                              camera_ids=[], zone_names=[], person_ids=[],
                                              days=[], cooldown_s=60, time_start="", time_end="",
                                              actions=[ACTION_EXAMPLES["homeassistant"]])
    if rule is None:
        raise HTTPException(404)
    cams = db.scalars(select(Camera).order_by(Camera.name)).all()
    zone_names = sorted({z.name for c in cams for z in c.zones} | {"full"})
    return render(request, "rule_form.html", rule=rule, cams=cams, zone_names=zone_names,
                  people=db.scalars(select(Person).order_by(Person.name)).all(),
                  event_types=EVENT_TYPES, action_types=ACTION_TYPES,
                  actions_json=json.dumps(rule.actions or [], indent=2),
                  examples_json=json.dumps(ACTION_EXAMPLES))


@router.post("/rules/save")
async def rule_save(request: Request, user: User = Depends(admin_user),
                    db: Session = Depends(get_db, scope="function")):
    form = await request.form()
    rid = form.get("id")
    rule = db.get(Rule, int(rid)) if rid else Rule()
    if rule is None:
        raise HTTPException(404)
    try:
        actions = json.loads(str(form.get("actions") or "[]"))
        if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
            raise ValueError("actions must be a JSON list of objects")
    except ValueError as err:
        flash(request, f"Invalid actions JSON: {err}", "error")
        return redirect(f"/rules/{rid}/edit" if rid else "/rules/new")
    rule.name = str(form.get("name") or "Rule").strip()[:128]
    rule.enabled = form.get("enabled") == "on"
    rule.event_types = [t for t in form.getlist("event_types") if t in EVENT_TYPES]
    rule.camera_ids = [int(x) for x in form.getlist("camera_ids")]
    rule.zone_names = [str(x) for x in form.getlist("zone_names")]
    rule.person_ids = [int(x) for x in form.getlist("person_ids")]
    rule.days = [int(x) for x in form.getlist("days")]
    rule.time_start = str(form.get("time_start") or "")
    rule.time_end = str(form.get("time_end") or "")
    rule.cooldown_s = max(0, int(form.get("cooldown_s") or 0))
    rule.actions = actions
    if not rid:
        db.add(rule)
    db.flush()
    flash(request, f"Saved rule {rule.name}")
    return redirect("/rules")


@router.post("/rules/{rid}/delete")
def rule_delete(request: Request, rid: int, user: User = Depends(admin_user),
                db: Session = Depends(get_db, scope="function")):
    r = db.get(Rule, rid)
    if r:
        db.delete(r)
    return redirect("/rules")


@router.post("/rules/{rid}/test")
def rule_test(request: Request, rid: int, user: User = Depends(admin_user),
              db: Session = Depends(get_db, scope="function")):
    r = db.get(Rule, rid)
    if r is None:
        raise HTTPException(404)
    runtime = rt(request)
    ev = Event(ts=datetime.now(timezone.utc), camera_name="Test camera", zone="test",
               type=(r.event_types or ["face_known"])[0], person_name="Test Person",
               confidence=0.99, extra={"test": True})
    db.add(ev)
    db.commit()
    ctx = runtime.event_context(ev.id)
    results = runtime.actions.run_rule(rule_view(r), ctx)
    msg = "; ".join(f"{'OK' if x.ok else 'FAIL'}: {x.detail[:150]}" for x in results) or "no actions"
    flash(request, f"Test of '{r.name}': {msg}", "ok" if all(x.ok for x in results) else "error")
    return redirect("/rules")


# --- Events -------------------------------------------------------------------

@router.get("/events")
def events(request: Request, camera: int | None = None, person: int | None = None,
           type: str = "", day: str = "", page: int = 1,
           user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    q = select(Event)
    if camera:
        q = q.where(Event.camera_id == camera)
    if person:
        q = q.where(Event.person_id == person)
    if type:
        q = q.where(Event.type == type)
    if day:
        try:
            d = date.fromisoformat(day)
            tz = rt(request).tz
            start = datetime.combine(d, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
            q = q.where(Event.ts >= start, Event.ts < start + timedelta(days=1))
        except ValueError:
            pass
    per = 50
    rows = db.scalars(q.order_by(Event.ts.desc()).offset((max(1, page) - 1) * per).limit(per)).all()
    logs: dict[int, list[ActionLog]] = {}
    if rows:
        for lg in db.scalars(select(ActionLog).where(ActionLog.event_id.in_([e.id for e in rows]))):
            logs.setdefault(lg.event_id, []).append(lg)
    return render(request, "events.html", events=rows, logs=logs, page=page, per=per,
                  cams=db.scalars(select(Camera).order_by(Camera.name)).all(),
                  people=db.scalars(select(Person).order_by(Person.name)).all(),
                  event_types=EVENT_TYPES,
                  f={"camera": camera, "person": person, "type": type, "day": day})


@router.get("/media/events/{eid}/{kind}")
def event_image(eid: int, kind: str, user: User = Depends(current_user),
                db: Session = Depends(get_db, scope="function")):
    ev = db.get(Event, eid)
    path = (ev.snapshot_path if kind == "snapshot" else ev.face_path) if ev else ""
    if not path or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/events/{eid}/delete")
def event_delete(eid: int, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    ev = db.get(Event, eid)
    if ev:
        for p in (ev.snapshot_path, ev.face_path):
            if p:
                Path(p).unlink(missing_ok=True)
        db.delete(ev)
    return redirect("/events")


# --- Settings -----------------------------------------------------------------

EDITABLE = {
    "archive.schedule_time": str, "archive.max_part_hours": float, "archive.gap_minutes": float,
    "archive.split_on_gaps": bool, "archive.keep_merged": str, "archive.settle_minutes": float,
    "youtube.category_id": str,
    "faces.threshold": float, "faces.dedupe_seconds": float,
    "llm.provider": str, "llm.model": str, "llm.base_url": str, "llm.max_calls_per_hour": int,
    "ha.url": str, "events.retention_days": int, "live.max_cameras": int,
}
SECRET_FIELDS = ("llm.api_key", "ha.token")


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(admin_user),
                  db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    values = {k: ss.get(db, k) for k in ss.DEFAULTS}
    masked = {k: ss.mask(values.get(k)) for k in SECRET_FIELDS}
    return render(request, "settings.html", v=values, masked=masked,
                  tz=settings_of(request).timezone, accounts=accounts_of(db),
                  library_path=lib_root(request, db))


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(admin_user),
                        db: Session = Depends(get_db, scope="function")):
    form = await request.form()
    for key, typ in EDITABLE.items():
        field = key.replace(".", "__")
        if typ is bool:
            ss.set_(db, key, form.get(field) == "on")
            continue
        if field not in form:
            continue
        raw = str(form.get(field)).strip()
        try:
            ss.set_(db, key, typ(raw) if raw != "" or typ is str else ss.DEFAULTS[key])
        except ValueError:
            flash(request, f"Invalid value for {key}", "error")
            return redirect("/settings")
    for key in SECRET_FIELDS:
        field = key.replace(".", "__")
        raw = str(form.get(field) or "").strip()
        if raw == "-":
            ss.set_(db, key, "")
        elif raw:
            ss.set_(db, key, raw)
    at = str(ss.get(db, "archive.schedule_time"))
    try:
        hh, mm = (int(x) for x in at.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        flash(request, "Schedule time must be HH:MM", "error")
        return redirect("/settings")
    db.commit()
    sched = request.app.state.scheduler
    if sched:
        from coeus.jobs.scheduler import reschedule_daily
        reschedule_daily(sched, at)
    rt(request).invalidate_config()
    rt(request).sync_monitors()
    flash(request, "Settings saved")
    return redirect("/settings")


@router.post("/settings/test/ha")
def test_ha(request: Request, user: User = Depends(admin_user), db: Session = Depends(get_db, scope="function")):
    import httpx
    url, token = ss.get(db, "ha.url"), ss.get(db, "ha.token")
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/", headers={"Authorization": f"Bearer {token}"},
                      timeout=8)
        ok = r.status_code == 200
        flash(request, f"Home Assistant: HTTP {r.status_code} {r.text[:120]}",
              "ok" if ok else "error")
    except Exception as err:  # noqa: BLE001
        flash(request, f"Home Assistant unreachable: {err}", "error")
    return redirect("/settings#ha")


@router.post("/settings/test/llm")
def test_llm(request: Request, user: User = Depends(admin_user)):
    runtime = rt(request)
    runtime.invalidate_config()
    provider = runtime.llm()
    if provider is None:
        flash(request, "No LLM provider selected", "error")
        return redirect("/settings#llm")
    import cv2
    import numpy as np
    img = np.full((240, 320, 3), 200, np.uint8)
    cv2.rectangle(img, (110, 60), (210, 200), (40, 40, 160), -1)
    ok, buf = cv2.imencode(".jpg", img)
    try:
        text = provider.describe(buf.tobytes(), "test", "test")
        flash(request, f"{provider.name}/{provider.model}: {text[:200]}")
    except Exception as err:  # noqa: BLE001
        flash(request, f"LLM test failed: {err}", "error")
    return redirect("/settings#llm")


# --- Users --------------------------------------------------------------------

@router.get("/users")
def users_page(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db, scope="function")):
    request.state.user = user
    users = db.scalars(select(User).order_by(User.username)).all() if user.is_admin else [user]
    return render(request, "users.html", users=users)


@router.post("/users")
def user_create(request: Request, username: str = Form(...), password: str = Form(...),
                is_admin: str = Form(""), user: User = Depends(admin_user),
                db: Session = Depends(get_db, scope="function")):
    username = username.strip()
    if len(password) < 8 or not username:
        flash(request, "Username required and password must be at least 8 characters", "error")
    elif db.scalar(select(User).where(User.username == username)):
        flash(request, "User already exists", "error")
    else:
        db.add(User(username=username, password_hash=hash_password(password),
                    is_admin=is_admin == "on"))
        flash(request, f"Created user {username}")
    return redirect("/users")


@router.post("/users/{uid}/delete")
def user_delete(request: Request, uid: int, user: User = Depends(admin_user),
                db: Session = Depends(get_db, scope="function")):
    if uid == user.id:
        flash(request, "You cannot delete yourself", "error")
    else:
        u = db.get(User, uid)
        if u:
            db.delete(u)
    return redirect("/users")


@router.post("/users/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    u = db.get(User, user.id)
    if not verify_password(current, u.password_hash):
        flash(request, "Current password is wrong", "error")
    elif len(new) < 8:
        flash(request, "New password must be at least 8 characters", "error")
    else:
        u.password_hash = hash_password(new)
        flash(request, "Password changed")
    return redirect("/users")


# --- JSON status for the dashboard -------------------------------------------------

@router.get("/api/status")
def api_status(request: Request, user: User = Depends(current_user)):
    runtime = rt(request)
    return {cid: {"state": m.status.state, "error": m.status.error,
                  "fps": round(m.status.fps, 1), "last_frame_at": m.status.last_frame_at,
                  "last_event_at": m.status.last_event_at}
            for cid, m in runtime.monitors.monitors.items()}
