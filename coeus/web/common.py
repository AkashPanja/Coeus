from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from coeus.config import get_settings
from coeus.security import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _localtime(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def _filesize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _duration(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    return f"{h}h {rem // 60:02d}m"


templates.env.filters["localtime"] = _localtime
templates.env.filters["filesize"] = _filesize
templates.env.filters["duration"] = _duration
templates.env.globals["csrf_token"] = csrf_token


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("flash", request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
