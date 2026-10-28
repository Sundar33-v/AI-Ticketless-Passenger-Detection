"""Create the SQLite database (tables + indexes) if needed and print per-table row counts.

    python scripts/init_db.py [--db PATH] [--config config/config.yaml]

The path follows the single DB-path rule (``--db`` > ``TPD_DB_PATH`` > ``database.path``).
Exit codes: 0 ok, 1 config/usage error, 3 database error.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Dict, Optional, Sequence

import _bootstrap  # noqa: F401  (puts the project root on sys.path)

from app.config import load_config, resolve_db_path
from app.database import Database, DatabaseError
from app.settings_schema import ConfigError

TABLES = ("system_settings", "passengers", "tickets", "detection_sessions", "detection_records",
          "boarding_events", "validation_events", "model_evaluations", "metric_evaluations", "alert_reads")


def table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Row count of every application table (fixed identifiers, never user input)."""
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES}


def build_parser() -> _bootstrap.ArgumentParser:
    parser = _bootstrap.ArgumentParser(description="Initialize the ticket / detection database.")
    parser.add_argument("--db", help="database file (default: TPD_DB_PATH or database.path from the config)")
    parser.add_argument("--config", help="config file (default: config/config.yaml or TPD_CONFIG)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        db_path = resolve_db_path(args.db, cfg)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    try:
        database = Database(db_path, cfg.database.busy_timeout_ms)
        database.initialize()
        conn = database.read_connection()
        try:
            counts = table_counts(conn)
        finally:
            conn.close()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"Database error ({db_path}): {exc}", file=sys.stderr)
        return 3
    print(f"Database: {db_path}")
    for table, count in counts.items():
        print(f"  {table:<20} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
