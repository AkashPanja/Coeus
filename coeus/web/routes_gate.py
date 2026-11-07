"""Gate page, its inbound webhook, and the 'try a photo' recognition test."""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from coeus.db import get_db
from coeus.gate import DEFAULT_ACTION, METHODS, MODES, GateConfig
from coeus.models import Camera, GateCheck, Person, User
from coeus.security import admin_user, current_user
from coeus.vision import faces as fr
from coeus.web.common import flash, redirect, render

router = APIRouter()
DB = Depends(get_db, scope="function")

OUTCOME_TEXT = {
    "opened": "Gate opened", "denied": "Known, not allowed", "no_match": "Not recognised",
    "no_face": "No face seen", "cooldown": "Skipped (just opened)", "busy": "Already checking",
    "error": "Error", "disabled": "Gate off",
}


def gate(request: Request):
    return request.app.state.runtime.gate


def hook_url(request: Request, token: str) -> str:
    base = request.app.state.settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/api/hooks/gate/{token}"


@router.get("/gate")
def gate_page(request: Request, user: User = Depends(admin_user), db: Session = DB):
    request.state.user = user
    cfg = gate(request).config()
    checks = db.scalars(select(GateCheck).order_by(GateCheck.ts.desc()).limit(30)).all()
    cams = db.scalars(select(Camera).order_by(Camera.name)).all()
    cam = db.get(Camera, cfg.camera_id) if cfg.camera_id else None
    return render(request, "gate.html", cfg=cfg, cams=cams, cam=cam, checks=checks,
                  people=db.scalars(select(Person).order_by(Person.name)).all(),
                  methods=METHODS, modes=MODES, outcome_text=OUTCOME_TEXT,
                  action_json=json.dumps(cfg.action, indent=2), hook=hook_url(request, cfg.token),
                  llm_ready=request.app.state.runtime.llm() is not None)


@router.post("/gate/save")
async def gate_save(request: Request, user: User = Depends(admin_user), db: Session = DB):
    form = await request.form()
    ctl = gate(request)
    cfg = ctl.config()
    try:
        action = json.loads(str(form.get("action") or "{}")) or dict(DEFAULT_ACTION)
        if not isinstance(action, dict) or action.get("type") not in ("homeassistant", "webhook"):
            raise ValueError('use {"type": "homeassistant", ...} or {"type": "webhook", ...}')
    except ValueError as err:
        flash(request, f"Gate action: {err}", "error")
        return redirect("/gate")
    cam_id = str(form.get("camera_id") or "")
    cfg.camera_id = int(cam_id) if cam_id.isdigit() and db.get(Camera, int(cam_id)) else None
    cfg.enabled = form.get("enabled") == "on" and cfg.camera_id is not None
    cfg.mode = str(form.get("mode") or "webhook")
    cfg.method = str(form.get("method") or "local")
    cfg.llm_image = "frame" if form.get("llm_image") == "frame" else "face"
    cfg.person_ids = [int(x) for x in form.getlist("person_ids")]
    num = lambda k, d, lo, hi: max(lo, min(hi, float(form.get(k) or d)))  # noqa: E731
    cfg.llm_min_confidence = num("llm_min_confidence", 0.75, 0.3, 0.99)
    cfg.max_llm_calls = int(num("max_llm_calls", 3, 0, 10))
    cfg.window_s = num("window_s", 15, 2, 60)
    cfg.cooldown_s = int(num("cooldown_s", 30, 0, 3600))
    cfg.action = action
    cfg = GateConfig.load(cfg.dump())  # re-validate mode/method
    ctl.save(cfg)
    note = ""
    cam = db.get(Camera, cfg.camera_id) if cfg.camera_id else None
    if cfg.enabled and cfg.mode in ("continuous", "both") and cam and not cam.monitor_enabled:
        note = (f" Continuous checking needs {cam.name} to be watched live: tick "
                "“Watch this camera live” on the camera.")
    if cfg.uses_llm and request.app.state.runtime.llm() is None:
        note += " The chosen method uses an LLM, but none is set up in Settings."
    flash(request, "Gate settings saved." + note, "error" if note else "ok")
    return redirect("/gate")


@router.post("/gate/token")
def gate_new_token(request: Request, user: User = Depends(admin_user)):
    ctl = gate(request)
    cfg = ctl.config()
    cfg.token = secrets.token_urlsafe(24)
    ctl.save(cfg)
    flash(request, "New webhook address created. Update it wherever you call it from.")
    return redirect("/gate")


@router.post("/gate/check")
def gate_check_now(request: Request, user: User = Depends(admin_user)):
    res = gate(request).check("manual")
    kind = "ok" if res["outcome"] in ("opened",) else "error"
    who = f" {res.get('person')} ({res.get('confidence')})" if res.get("person") else ""
    flash(request, f"{OUTCOME_TEXT.get(res['outcome'], res['outcome'])}{who}. "
                   f"{res.get('detail', '')}", kind)
    return redirect("/gate")


@router.post("/gate/test-open")
def gate_test_open(request: Request, user: User = Depends(admin_user)):
    ctl = gate(request)
    res = ctl.open_gate(ctl.config(), "Test", 1.0)
    flash(request, f"Gate action {'worked' if res.ok else 'failed'}: {res.detail[:200]}",
          "ok" if res.ok else "error")
    return redirect("/gate")


@router.post("/gate/try-photo")
async def gate_try_photo(request: Request, photo: UploadFile = File(...),
                         use_llm: str = Form(""), user: User = Depends(admin_user)):
    """Recognise a photo the way the gate would, without opening anything."""
    runtime = request.app.state.runtime
    ctl = gate(request)
    data = await photo.read()
    img = fr.decode_image(data) if len(data) < 15 * 1024 * 1024 else None
    if img is None:
        return JSONResponse({"ok": False, "error": "That file is not a readable image."})
    out: dict = {"ok": True, "local": None, "llm": None}
    try:
        backend = runtime.face_backend()
        faces = sorted(backend.detect(img), key=lambda f: f.bbox[2] * f.bbox[3], reverse=True)
        out["faces"] = len(faces)
        if faces:
            m = runtime.gallery.match(backend.embed(img, faces[0]),
                                      float(runtime._config().get("faces.threshold") or 0.363))
            out["local"] = {"name": m.name if m.known else None, "confidence": round(m.score, 3)}
    except Exception as err:  # noqa: BLE001
        out["local_error"] = str(err)
    if use_llm:
        try:
            cfg = ctl.config()
            pic = img
            if cfg.llm_image == "face" and out.get("faces"):
                pic = fr.crop(img, faces[0].bbox, pad=0.4)
            pid, name, conf, reason = ctl._llm_identify(pic)
            out["llm"] = {"name": name or None, "confidence": round(conf, 3), "reason": reason}
        except Exception as err:  # noqa: BLE001
            out["llm_error"] = str(err)
    return JSONResponse(out)


@router.get("/media/gate/{cid}")
def gate_image(cid: int, user: User = Depends(current_user), db: Session = DB):
    row = db.get(GateCheck, cid)
    if row is None or not row.snapshot_path or not Path(row.snapshot_path).exists():
        raise HTTPException(404)
    return FileResponse(row.snapshot_path, media_type="image/jpeg")


# --- Inbound webhook ------------------------------------------------------------

@router.api_route("/api/hooks/gate/{token}", methods=["GET", "POST"])
def gate_hook(request: Request, token: str, wait: bool = True):
    """Start a gate check. Call it from Home Assistant, a doorbell, n8n, curl...

    wait=true (default) answers when the check ends (up to the time window) with the result;
    wait=false answers at once with 202 and checks in the background.
    """
    ctl = gate(request)
    cfg = ctl.config()
    if not hmac.compare_digest(token, cfg.token):
        raise HTTPException(404)
    if not cfg.enabled or cfg.mode not in ("webhook", "both"):
        return JSONResponse({"outcome": "disabled",
                             "detail": "Webhook checks are switched off on the Gate page."},
                            status_code=409)
    if not wait:
        threading.Thread(target=ctl.check, args=("webhook",), daemon=True).start()
        return JSONResponse({"outcome": "started"}, status_code=202)
    return JSONResponse(ctl.check("webhook"))
