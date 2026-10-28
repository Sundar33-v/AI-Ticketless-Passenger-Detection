"""Persistent conductor read status of derived alert groups (table ``alert_reads``, no Streamlit).

Notifications are not stored: they are derived live by ``app.alert_service.alert_groups``.
This module stores only how far each group (by ``AlertGroup.key``) was read, as the
group's ``last_at`` at the time of the mark. It is the only writer of ``alert_reads``;
the dashboard calls it from ``dashboard/components/notifications.py`` on an explicit
Mark as read / Mark all as read / Notify Conductor.

Invariant: read state is monotonic. ``mark_read`` keeps ``max(last_at_seen)`` in SQL
(fixed-width ISO strings compare chronologically), so a later event in the same group
(``last_at > last_at_seen``) makes the group unread again, exactly as
``app.alert_service.unread_count`` expects.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.database import parse_iso, to_iso, transaction

logger = logging.getLogger(__name__)

MAX_KEY = 200
CHUNK = 500  # keys per SELECT (well below SQLite's bound-parameter limit)

ReadItem = Tuple[str, str]  # (alert group key, to_iso(group.last_at))

_missing_table_logged = False


def _valid_key(key: object) -> bool:
    return (isinstance(key, str) and 1 <= len(key) <= MAX_KEY
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in key))


def _normalized(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return to_iso(parse_iso(value))
    except (ValueError, TypeError, OverflowError):
        return None


def valid_items(items: Iterable[object]) -> List[ReadItem]:
    """The well-formed ``(key, iso)`` pairs of ``items``, max timestamp per key, sorted by key (pure).

    A key must be a ``str`` of 1..``MAX_KEY`` characters without control characters and the
    timestamp must parse with ``parse_iso``; it is re-normalized with ``to_iso``. Every other
    item is skipped and logged at WARNING. This is the single validation point for read items.
    """
    best: Dict[str, str] = {}
    for item in items:
        try:
            key, stamp = item  # type: ignore[misc]
        except (TypeError, ValueError):
            key, stamp = None, None
        iso = _normalized(stamp)
        if not _valid_key(key) or iso is None:
            logger.warning("Ignored invalid alert read item: %r", item)
            continue
        if key not in best or iso > best[key]:
            best[key] = iso
    return sorted(best.items())


def _is_missing_table(exc: sqlite3.Error) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc).lower()


def load_read_state(conn: sqlite3.Connection, keys: Sequence[str]) -> Dict[str, str]:
    """``{alert_key: last_at_seen}`` for the stored keys among ``keys`` (parameterized, chunks of 500).

    A database without the ``alert_reads`` table (opened before the schema was upgraded) gives
    ``{}`` and one WARNING per process; any other ``sqlite3.Error`` propagates.
    """
    global _missing_table_logged
    unique = list(dict.fromkeys(k for k in keys if isinstance(k, str)))
    found: Dict[str, str] = {}
    try:
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start:start + CHUNK]
            marks = ", ".join("?" for _ in chunk)
            for key, seen in conn.execute(
                    f"SELECT alert_key, last_at_seen FROM alert_reads WHERE alert_key IN ({marks})", chunk):
                found[str(key)] = str(seen)
    except sqlite3.Error as exc:
        if not _is_missing_table(exc):
            raise
        if not _missing_table_logged:
            _missing_table_logged = True
            logger.warning("alert_reads table is missing; saved read status is unavailable "
                           "(restart the dashboard after upgrading): %s", exc)
        return {}
    return found


def mark_read(conn: sqlite3.Connection, items: Sequence[object], now: datetime) -> int:
    """Store ``items`` as read (UPSERT keeping the newest ``last_at_seen``); returns rows written.

    Items are validated with :func:`valid_items` first, so any caller is safe; nothing valid
    means no SQL at all. Runs in one ``transaction(conn)``.
    """
    clean = valid_items(items)
    if not clean:
        return 0
    read_at = to_iso(now)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO alert_reads (alert_key, last_at_seen, read_at) VALUES (?, ?, ?) "
            "ON CONFLICT(alert_key) DO UPDATE SET "
            "last_at_seen = max(alert_reads.last_at_seen, excluded.last_at_seen), read_at = excluded.read_at",
            [(key, seen, read_at) for key, seen in clean])
    logger.info("Marked %d alert(s) as read", len(clean))
    return len(clean)
