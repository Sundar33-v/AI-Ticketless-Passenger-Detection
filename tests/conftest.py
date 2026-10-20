"""Shared pytest fixtures and repository guards.

* ``isolated_env`` (autouse): no ``TPD_*`` variable or ``.env`` file from the
  developer machine leaks into a test, and any code path that forgets ``--db``
  lands in the test's own temp directory (``TPD_DB_PATH``), never in ``data/``.
* ``protect_repo_dbs`` (autouse, session): proves the legacy database
  ``data/passengers.db`` is untouched and ``data/ticketless.db`` is not created.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
for _path in (ROOT, SCRIPTS_DIR):  # scripts/*.py are imported by name (import init_db, ...)
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import app.config  # noqa: E402
from app.settings_schema import AppConfig  # noqa: E402

DATA_DIR = ROOT / "data"
LEGACY_DB = DATA_DIR / "passengers.db"
LEGACY_WAL = DATA_DIR / "passengers.db-wal"
DEFAULT_DB = DATA_DIR / "ticketless.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:  # read-only; never sqlite3.connect() to these files
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint() -> dict:
    prints: dict = {"ticketless_db_exists": DEFAULT_DB.exists()}
    if LEGACY_DB.exists():
        stat = LEGACY_DB.stat()
        prints["passengers.db"] = (stat.st_size, stat.st_mtime_ns, _sha256(LEGACY_DB))
    if LEGACY_WAL.exists():
        prints["passengers.db-wal"] = (LEGACY_WAL.stat().st_size, _sha256(LEGACY_WAL))
    return prints


@pytest.fixture(autouse=True, scope="session")
def protect_repo_dbs():
    """Fail the run if the tests touched the repository databases.

    ``data/passengers.db`` is checked by size, mtime_ns and SHA-256 and its
    ``-wal`` (if present) by size and SHA-256. ``-shm`` is deliberately not
    fingerprinted: the file is open in the user's editor, and SQLite readers
    update read-marks in the wal-index (``-shm``) on their own, so it can change
    without any write by our code.
    """
    before = _fingerprint()
    yield
    after = _fingerprint()
    for key in ("passengers.db", "passengers.db-wal"):
        assert after.get(key) == before.get(key), f"data/{key} was modified by the test run"
    if not before["ticketless_db_exists"]:
        assert not DEFAULT_DB.exists(), "data/ticketless.db was created by the test run"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    import os

    for name in list(os.environ):
        if name.startswith("TPD_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app.config, "DOTENV_PATH", tmp_path / "missing.env")
    monkeypatch.setenv("TPD_DB_PATH", str(tmp_path / "default.db"))
    yield


@pytest.fixture
def cfg() -> AppConfig:
    """Built-in defaults (default route R1, station City Hospital), independent of config.yaml."""
    return AppConfig.from_dict({})


# 2026-01-15 11:00 in Asia/Kolkata (the default app.timezone).
FIXED_NOW = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    """An initialized file database in the test's temp directory."""
    from app.database import Database

    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


@pytest.fixture
def open_dashboard(monkeypatch):
    """Factory: ``open_dashboard(db_path, page="app.py", clear_cache=True) -> AppTest``.

    Points ``TPD_DB_PATH`` at the test's DB and clears Streamlit's caches so each
    test sees only its own database. File logging is disabled so no ``logs/``
    directory is created in the repository.
    """
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(app.config, "configure_logging", lambda *args, **kwargs: None)

    def _open(db_path, page: str = "app.py", clear_cache: bool = True):
        monkeypatch.setenv("TPD_DB_PATH", str(db_path))
        if clear_cache:
            st.cache_resource.clear()
            st.cache_data.clear()
        script = ROOT / "dashboard" / ("app.py" if page == "app.py" else f"pages/{page}")
        return AppTest.from_file(str(script), default_timeout=30)

    return _open


@pytest.fixture
def dashboard_db(tmp_path):
    """Path of the database the dashboard fixtures use (created on first use)."""
    return tmp_path / "dashboard.db"


@pytest.fixture
def dashboard_app(open_dashboard, dashboard_db):
    """AppTest of dashboard/app.py reading ``dashboard_db`` (call ``.run()`` after preparing the DB)."""
    return open_dashboard(dashboard_db)


@pytest.fixture
def journey(cfg):
    """Default journey (R1 outbound at City Hospital) built via build_journey at FIXED_NOW."""
    from app.ticket_validator import build_journey

    return build_journey(cfg, now=FIXED_NOW)
