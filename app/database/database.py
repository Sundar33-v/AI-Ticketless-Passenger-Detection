"""SQLite engine and session management for passenger events.

Usage::

    from app.database import init_db, PassengerRepository

    db = init_db()                      # data/passengers.db (or $TICKETLESS_DB_PATH)
    repo = PassengerRepository(db)

``init_db(":memory:")`` gives an isolated in-memory database (used by tests).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "passengers.db"
DB_PATH_ENV_VAR = "TICKETLESS_DB_PATH"
IN_MEMORY = ":memory:"

PathLike = Union[str, Path]


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def build_sqlite_url(db_path: Optional[PathLike] = None) -> str:
    """Return a SQLAlchemy SQLite URL.

    Resolution order: explicit ``db_path`` -> ``$TICKETLESS_DB_PATH`` -> ``data/passengers.db``.
    The parent directory is created for file databases.
    """
    if db_path is None or str(db_path) == "":
        db_path = os.environ.get(DB_PATH_ENV_VAR) or DEFAULT_DB_PATH

    if str(db_path) == IN_MEMORY:
        return f"sqlite+pysqlite:///{IN_MEMORY}"

    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Per-connection pragmas.

    WAL lets the dashboard read while the detection pipeline writes;
    busy_timeout avoids immediate 'database is locked' errors under contention.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")  # no-op for in-memory DBs
    finally:
        cursor.close()


class Database:
    """Owns the engine and session factory for one SQLite database."""

    def __init__(self, db_path: Optional[PathLike] = None, echo: bool = False) -> None:
        self.url = build_sqlite_url(db_path)
        self.is_memory = self.url.endswith(IN_MEMORY)

        engine_kwargs = {
            "echo": echo,
            # Camera/tracking loops may run on other threads than the one that
            # created the engine; sessions themselves must not be shared.
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
        if self.is_memory:
            # One shared connection, otherwise each connection sees an empty DB.
            engine_kwargs["poolclass"] = StaticPool

        self.engine = create_engine(self.url, **engine_kwargs)
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_tables(self) -> None:
        # Import registers the models on Base.metadata.
        from app.database import models  # noqa: F401

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, rollback on error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close pooled connections (required before deleting the DB file on Windows)."""
        self.engine.dispose()


def init_db(db_path: Optional[PathLike] = None, echo: bool = False) -> Database:
    """Create the database (if needed) and all tables, and return a ``Database``."""
    db = Database(db_path, echo=echo)
    db.create_tables()
    return db
