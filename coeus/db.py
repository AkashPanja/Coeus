from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(url: str):
    global _engine, _SessionLocal
    _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    from coeus import models  # noqa: F401  (register tables)

    Base.metadata.create_all(_engine)
    return _engine


def get_engine():
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    assert _SessionLocal is not None, "init_engine() not called"
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
