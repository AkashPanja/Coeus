"""Coeus entry point: FastAPI app with background workers."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from coeus.config import Settings, get_settings
from coeus.db import init_engine, session_scope
from coeus.security import LoginRequired, ensure_admin, verify_csrf

log = logging.getLogger("coeus")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    init_engine(settings.db_url)
    if settings.secret_key == "change-me":
        log.warning("COEUS_SECRET_KEY is not set; sessions use an insecure default key")

    with session_scope() as db:
        generated = ensure_admin(db, settings.admin_user, settings.admin_password)
    if generated:
        log.warning("Created admin user %r with generated password: %s  (change it after login)",
                    settings.admin_user, generated)

    from coeus.services import Runtime
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        runtime.reload_gallery()
        if not settings.disable_workers:
            from coeus.jobs import archive
            from coeus.jobs.scheduler import build_scheduler
            archive.recover_interrupted()
            sched = build_scheduler(settings)
            sched.start()
            app.state.scheduler = sched
            runtime.sync_monitors()
        yield
        if sched:
            sched.shutdown(wait=False)
        runtime.shutdown()

    app = FastAPI(title="Coeus", lifespan=lifespan, docs_url=None, redoc_url=None,
                  dependencies=[Depends(verify_csrf)])
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = None
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                       session_cookie="coeus_session", same_site="lax",
                       max_age=settings.session_max_age_days * 86400, https_only=False)

    from pathlib import Path
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
              name="static")

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return JSONResponse({"ok": True})

    from coeus.web import routes
    app.include_router(routes.router)
    return app


def run() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("coeus.main:create_app", factory=True, host="0.0.0.0", port=8765,
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":  # pragma: no cover
    run()
