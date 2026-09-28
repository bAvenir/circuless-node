"""Database engine and sessions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from . import tenancy as _tenancy  # noqa: F401
from .settings import Settings

# The import above is for its side effect, and it is load-bearing: importing `tenancy`
# registers the session listeners that scope every tenant-owned query (N4). It lives here
# because everything that makes a session comes through this module, so there is no path
# to a database that skips it. Without the import the node would run with tenant
# isolation silently switched off.


def create_db_engine(settings: Settings) -> Engine:
    is_sqlite = settings.database_url.startswith("sqlite")

    if is_sqlite:
        # The URL may point anywhere; make sure the directory exists before SQLite tries to
        # create the file, otherwise the first start fails on a fresh install.
        path = settings.database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        settings.database_url,
        # check_same_thread is a SQLite-only guard against sharing a connection between
        # threads; FastAPI's threadpool does exactly that, and the pool keeps it safe.
        connect_args={"check_same_thread": False} if is_sqlite else {},
        pool_pre_ping=True,
    )

    if is_sqlite:
        _enable_sqlite_pragmas(engine)
    return engine


def _enable_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # WAL lets the sync client write while requests read, which matters because the
        # node keeps serving from cache during a Cloud outage (F16).
        cursor.execute("PRAGMA journal_mode=WAL")
        # SQLite does not enforce foreign keys unless asked, per connection.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def session_factory(engine: Engine):
    def get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    return get_session
