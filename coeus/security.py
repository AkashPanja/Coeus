"""Password hashing, session auth, CSRF and login throttling."""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from coeus.db import get_db
from coeus.models import User

log = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


class LoginThrottle:
    """Lock out a (ip, username) pair after N failures for `lock_seconds`."""

    def __init__(self, max_failures: int = 5, lock_seconds: int = 300):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str, username: str) -> str:
        return f"{ip}|{username.lower()}"

    def is_locked(self, ip: str, username: str) -> bool:
        now = time.monotonic()
        with self._lock:
            fails = [t for t in self._fails.get(self._key(ip, username), [])
                     if now - t < self.lock_seconds]
            self._fails[self._key(ip, username)] = fails
            return len(fails) >= self.max_failures

    def fail(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.setdefault(self._key(ip, username), []).append(time.monotonic())

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.pop(self._key(ip, username), None)


throttle = LoginThrottle()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        # Spend similar time to avoid user enumeration by timing.
        verify_password(password, "$2b$12$" + "a" * 53)
        return None
    return user if verify_password(password, user.password_hash) else None


def ensure_admin(db: Session, username: str, password: str) -> str | None:
    """Create the bootstrap admin if no users exist. Returns a generated password if any."""
    if db.scalar(select(User).limit(1)) is not None:
        return None
    generated = None
    if not password:
        password = generated = secrets.token_urlsafe(12)
    db.add(User(username=username, password_hash=hash_password(password), is_admin=True))
    db.flush()
    return generated


# --- CSRF -----------------------------------------------------------------

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.url.path.startswith("/api/hooks/"):
        return  # machine-to-machine; authenticated by the secret token in the URL
    expected = request.session.get("csrf", "")
    sent = request.headers.get("x-csrf-token", "")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            sent = str(form.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(expected, sent):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


# --- Current user -----------------------------------------------------------

class LoginRequired(Exception):
    """Raised for browser requests without a session; handled by redirect to /login."""


def current_user(request: Request, db: Session = Depends(get_db, scope="function")) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        raise LoginRequired()
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
