"""Config and database access for the dashboard.

The dashboard uses the same DB path rule as the detection process and the
scripts (``app.config.resolve_db_path``: ``TPD_DB_PATH`` > ``database.path``);
there is deliberately no path input widget. ``Database`` holds only the path,
so caching it per resolved path is thread-safe; every render opens its own
connection and closes it.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Tuple

import streamlit as st

import app.config as app_config
from app.config import load_config, resolve_db_path
from app.database import Database, DatabaseError
from app.settings_schema import AppConfig, ConfigError


def get_config() -> AppConfig:
    return _get_config(os.environ.get("TPD_CONFIG"), os.environ.get("TPD_DB_PATH"))


@st.cache_resource(show_spinner=False)
def _get_config(config_path: str | None, db_env: str | None) -> AppConfig:
    # db_env is unused in the body; it is part of the cache key so a changed TPD_DB_PATH gives a new entry.
    cfg = load_config(config_path)
    app_config.configure_logging(cfg.logging, filename="dashboard.log")
    return cfg


def db_path() -> str:
    return str(resolve_db_path(cfg=get_config()))


def get_database() -> Database:
    return _get_database(db_path(), get_config().database.busy_timeout_ms)


@st.cache_resource(show_spinner=False)
def _get_database(path: str, busy_timeout_ms: int) -> Database:
    database = Database(path, busy_timeout_ms)
    database.initialize()  # creates tables only; the main page writes no records
    return database


def require_database() -> Tuple[AppConfig, Database]:
    """Config + initialized database, or an error message and ``st.stop()``."""
    try:
        return get_config(), get_database()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        st.error(f"Database error: {exc}")
    st.stop()
    raise AssertionError("unreachable")  # st.stop() raises


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    """A new read-only connection, closed afterwards."""
    conn = get_database().read_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn() -> Iterator[sqlite3.Connection]:
    """A new read-write connection (Ticket Check page only), closed afterwards."""
    conn = get_database().connect()
    try:
        yield conn
    finally:
        conn.close()
