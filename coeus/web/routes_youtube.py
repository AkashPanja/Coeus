"""YouTube accounts (several channels / Google projects) and the recordings-library picker."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from coeus import settings_store as ss
from coeus.db import get_db, session_scope
from coeus.jobs import archive
from coeus.models import Camera, User, YouTubeAccount
from coeus.recordings import library
from coeus.security import admin_user
from coeus.web.common import flash, redirect, render
from coeus.youtube import uploader as yt

router = APIRouter()
DB = Depends(get_db, scope="function")


def _safe_next(url: str, default: str = "/youtube") -> str:
    return url if url.startswith("/") and not url.startswith("//") else default


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


# --- Accounts -----------------------------------------------------------------

@router.get("/youtube")
def accounts_page(request: Request, user: User = Depends(admin_user), db: Session = DB):
    request.state.user = user
    accounts = list(db.scalars(select(YouTubeAccount).order_by(YouTubeAccount.name)))
    cams: dict[int, list[str]] = {}
    for c in db.scalars(select(Camera).where(Camera.yt_account_id.is_not(None))):
        cams.setdefault(c.yt_account_id, []).append(c.name)
    return render(request, "youtube_accounts.html", accounts=accounts, cams=cams,
                  quotas={a.id: archive.quota_state(db, a) for a in accounts})


@router.get("/youtube/new")
def account_new(request: Request, next: str = "/youtube", user: User = Depends(admin_user),
                db: Session = DB):
    request.state.user = user
    accounts = list(db.scalars(select(YouTubeAccount).order_by(YouTubeAccount.name)))
    clients = {}
    for a in accounts:
        if a.client_id and a.client_id not in clients:
            clients[a.client_id] = a
    return render(request, "youtube_new.html", next=_safe_next(next),
                  reuse=list(clients.values()))


@router.post("/youtube/new")
def account_create(request: Request, name: str = Form(...), client_json: str = Form(""),
                   reuse_from: str = Form(""), daily_upload_limit: int = Form(6),
                   next: str = Form("/youtube"), user: User = Depends(admin_user),
                   db: Session = DB):
    name = name.strip()[:128]
    back = f"/youtube/new?next={quote(_safe_next(next))}"
    if not name or db.scalar(select(YouTubeAccount).where(YouTubeAccount.name == name)):
        flash(request, "Give the account a unique name", "error")
        return redirect(back)
    try:
        if reuse_from.isdigit() and not client_json.strip():
            src = db.get(YouTubeAccount, int(reuse_from))
            if src is None:
                raise ValueError("unknown account to reuse")
            cfg = src.client_config
        else:
            cfg = yt.normalise_client_config(client_json)
    except ValueError as err:
        flash(request, f"OAuth client: {err}", "error")
        return redirect(back)
    acct = YouTubeAccount(name=name, client_config=cfg,
                          daily_upload_limit=max(1, min(100, daily_upload_limit)))
    db.add(acct)
    db.flush()
    db.commit()
    return _start_auth(request, acct, next)


def _start_auth(request: Request, acct: YouTubeAccount, next: str):
    try:
        url, _state, verifier = yt.start_authorization(acct.client_config)
    except Exception as err:  # noqa: BLE001
        flash(request, f"Could not build the Google sign-in link: {err}", "error")
        return redirect("/youtube")
    request.session[f"yt_verifier_{acct.id}"] = verifier
    request.session[f"yt_url_{acct.id}"] = url
    return redirect(f"/youtube/{acct.id}/connect?next={quote(_safe_next(next))}")


@router.post("/youtube/{aid}/reconnect")
def account_reconnect(request: Request, aid: int, next: str = Form("/youtube"),
                      user: User = Depends(admin_user), db: Session = DB):
    acct = db.get(YouTubeAccount, aid)
    if acct is None:
        raise HTTPException(404)
    return _start_auth(request, acct, next)


@router.get("/youtube/{aid}/connect")
def account_connect_page(request: Request, aid: int, next: str = "/youtube",
                         user: User = Depends(admin_user), db: Session = DB):
    request.state.user = user
    acct = db.get(YouTubeAccount, aid)
    if acct is None:
        raise HTTPException(404)
    url = request.session.get(f"yt_url_{aid}")
    if not url:
        return _start_auth(request, acct, next)
    return render(request, "youtube_connect.html", acct=acct, auth_url=url,
                  next=_safe_next(next))


@router.post("/youtube/{aid}/connect")
def account_connect(request: Request, aid: int, redirect_url: str = Form(...),
                    next: str = Form("/youtube"), user: User = Depends(admin_user),
                    db: Session = DB):
    acct = db.get(YouTubeAccount, aid)
    verifier = request.session.get(f"yt_verifier_{aid}")
    if acct is None:
        raise HTTPException(404)
    if not verifier:
        flash(request, "The sign-in link expired; here is a new one.", "error")
        return _start_auth(request, acct, next)
    try:
        acct.credentials = yt.finish_authorization(acct.client_config, verifier, redirect_url)
    except Exception as err:  # noqa: BLE001
        flash(request, f"Google did not accept that: {err}", "error")
        return redirect(f"/youtube/{aid}/connect?next={quote(_safe_next(next))}")
    request.session.pop(f"yt_verifier_{aid}", None)
    request.session.pop(f"yt_url_{aid}", None)
    db.commit()
    result = _run_test(db, acct)
    flash(request, f"{acct.name}: {result['detail']}", "ok" if result["ok"] else "error")
    return redirect(_safe_next(next))


def _run_test(db: Session, acct: YouTubeAccount) -> dict:
    aid = acct.id

    def save(info: dict) -> None:
        with session_scope() as db2:
            a = db2.get(YouTubeAccount, aid)
            if a:
                a.credentials = info

    result = yt.test_credentials(acct.credentials, save)
    db.refresh(acct)
    acct.last_test_ok = result["ok"]
    acct.last_test_at = datetime.now(timezone.utc)
    acct.last_test_detail = result["detail"]
    if result["channel_title"]:
        acct.channel_title, acct.channel_id = result["channel_title"], result["channel_id"]
    blocked, count, cap = archive.quota_state(db, acct)
    result["quota"] = f"{count}/{cap} uploads today" + (" · quota exhausted" if blocked else "")
    db.commit()
    return result


@router.post("/youtube/{aid}/test")
def account_test(request: Request, aid: int, user: User = Depends(admin_user), db: Session = DB):
    acct = db.get(YouTubeAccount, aid)
    if acct is None:
        raise HTTPException(404)
    result = _run_test(db, acct)
    if _wants_json(request):
        return JSONResponse(result)
    flash(request, f"{acct.name}: {result['detail']} ({result['quota']})",
          "ok" if result["ok"] else "error")
    return redirect("/youtube")


@router.post("/youtube/{aid}/update")
def account_update(request: Request, aid: int, name: str = Form(...),
                   daily_upload_limit: int = Form(6), user: User = Depends(admin_user),
                   db: Session = DB):
    acct = db.get(YouTubeAccount, aid)
    if acct is None:
        raise HTTPException(404)
    name = name.strip()[:128]
    clash = db.scalar(select(YouTubeAccount).where(YouTubeAccount.name == name,
                                                    YouTubeAccount.id != aid))
    if name and not clash:
        acct.name = name
    acct.daily_upload_limit = max(1, min(100, daily_upload_limit))
    flash(request, f"Saved {acct.name}")
    return redirect("/youtube")


@router.post("/youtube/{aid}/delete")
def account_delete(request: Request, aid: int, user: User = Depends(admin_user),
                   db: Session = DB):
    acct = db.get(YouTubeAccount, aid)
    if acct:
        affected = list(db.scalars(select(Camera).where(Camera.yt_account_id == aid)))
        for cam in affected:
            cam.yt_account_id = None
            cam.archive_enabled = False
        db.delete(acct)
        msg = f"Removed {acct.name}"
        if affected:
            msg += (f". Archiving turned off for {', '.join(c.name for c in affected)}: "
                    "choose another account for them.")
        flash(request, msg)
    return redirect("/youtube")


# --- Recordings library picker ---------------------------------------------------

@router.get("/library")
def library_page(request: Request, path: str | None = None, user: User = Depends(admin_user),
                 db: Session = DB):
    request.state.user = user
    mount = request.app.state.settings.recordings_dir
    current = str(ss.get(db, "library.root") or "")
    if not mount.is_dir():
        return render(request, "library.html", mount=mount, missing=True, current=current,
                      here=None, children=[], crumbs=[], suggestions=[])
    browse_rel = current if path is None else path
    try:
        here, children = library.browse(mount, browse_rel)
    except (library.OutsideMount, FileNotFoundError):
        here, children = library.browse(mount, "")
    crumbs, acc = [("/ (mounted path)", "")], []
    for part in [p for p in here.rel.split("/") if p]:
        acc.append(part)
        crumbs.append((part, "/".join(acc)))
    return render(request, "library.html", mount=mount, missing=False, current=current,
                  current_info=library.describe(mount, library.library_root(mount, current)),
                  here=here, children=children, crumbs=crumbs,
                  suggestions=library.detect_libraries(mount)[:8])


@router.post("/library/select")
def library_select(request: Request, path: str = Form(""), user: User = Depends(admin_user),
                   db: Session = DB):
    mount = request.app.state.settings.recordings_dir
    try:
        root, explanation = library.normalise_choice(mount, path)
    except (ValueError, FileNotFoundError) as err:
        flash(request, str(err), "error")
        return redirect(f"/library?path={quote(path)}")
    ss.set_(db, "library.root", root)
    flash(request, explanation + " Now choose a YouTube account for each camera you want "
                                 "archived.")
    return redirect("/recordings")
