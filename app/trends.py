"""Chart data: detection trend, ticket-check outcome distribution and failure reasons (pure, read-only).

Every function takes a :class:`~app.analytics.Period` and uses ``P()``, so the
charts use exactly the same boundaries as the KPI cards and tables.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from app.alert_service import FAILURE_OUTCOMES, outcome_label
from app.analytics import INVALID_OUTCOMES, OUTCOMES, P, Period, credential_key
from app.database import parse_iso, to_iso

SERIES = ("Persons detected", "Boardings", "Unverified boardings")
TREND_ROW_CAP = 200_000
HOUR, DAY, WEEK = "hour", "day", "week"


class TrendTooLargeError(RuntimeError):
    """More rows than a chart can sensibly show for the period."""


def bucket_size(start: datetime, end: datetime) -> str:
    """1 hour up to 48 h, 1 local day up to 14 days, otherwise 1 local week (Monday)."""
    length = end - start
    if length <= timedelta(hours=48):
        return HOUR
    if length <= timedelta(days=14):
        return DAY
    return WEEK


def _floor(value: datetime, size: str, tz: ZoneInfo) -> datetime:
    local = value.astimezone(tz)
    if size == HOUR:
        return local.replace(minute=0, second=0, microsecond=0)
    day: date = local.date()
    if size == WEEK:
        day -= timedelta(days=day.weekday())
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def _next(bucket: datetime, size: str, tz: ZoneInfo) -> datetime:
    if size == HOUR:
        return _floor(bucket.astimezone(timezone.utc) + timedelta(hours=1), HOUR, tz)
    step = 7 if size == WEEK else 1
    return datetime.combine(bucket.date() + timedelta(days=step), time(0, 0), tzinfo=tz)


def _earliest(conn: sqlite3.Connection) -> Optional[datetime]:
    row = conn.execute("SELECT MIN(t) FROM (SELECT MIN(detected_at) AS t FROM detection_records "
                       "UNION ALL SELECT MIN(boarded_at) FROM boarding_events)").fetchone()
    return parse_iso(row[0]) if row and row[0] else None


def detection_trend(conn: sqlite3.Connection, period: Period, tz: ZoneInfo, now: datetime) -> pd.DataFrame:
    """Columns ``bucket`` (aware local datetime, bucket start), ``series``, ``count``; zero-filled.

    "Persons detected" = distinct (session, Track ID) seen in the bucket;
    "Boardings" = boardings in the bucket; "Unverified boardings" = those whose
    current verification status is not VALID (the boarding <-> check link).
    """
    columns = ["bucket", "series", "count"]
    params = period.params()
    total = conn.execute(
        "SELECT (SELECT COUNT(*) FROM detection_records WHERE " + P("detected_at") + ") + "
        "(SELECT COUNT(*) FROM boarding_events WHERE " + P("boarded_at") + ")", params).fetchone()[0]
    if int(total) > TREND_ROW_CAP:
        raise TrendTooLargeError(f"{total} rows in this period")
    start = parse_iso(period.start) if period.start else _earliest(conn)
    if start is None:
        return pd.DataFrame(columns=columns)
    end = min(parse_iso(period.end), now) if period.end else now
    if end <= start:
        return pd.DataFrame(columns=columns)
    size = bucket_size(start, end)
    floor: Callable[[str], datetime] = lambda text: _floor(parse_iso(text), size, tz)  # noqa: E731

    persons: Dict[datetime, Set[Tuple[str, int]]] = {}
    for sid, track, at in conn.execute("SELECT session_id, track_id, detected_at FROM detection_records WHERE "
                                       + P("detected_at"), params):
        persons.setdefault(floor(at), set()).add((sid, int(track)))
    boardings: Dict[datetime, int] = {}
    unverified: Dict[datetime, int] = {}
    for at, status in conn.execute("SELECT boarded_at, verification_status FROM boarding_events WHERE "
                                   + P("boarded_at"), params):
        key = floor(at)
        boardings[key] = boardings.get(key, 0) + 1
        if status != "VALID":
            unverified[key] = unverified.get(key, 0) + 1

    rows: List[Tuple[datetime, str, int]] = []
    bucket, last = _floor(start, size, tz), _floor(end - timedelta(microseconds=1), size, tz)
    while bucket <= last:
        rows += [(bucket, SERIES[0], len(persons.get(bucket, ()))), (bucket, SERIES[1], boardings.get(bucket, 0)),
                 (bucket, SERIES[2], unverified.get(bucket, 0))]
        bucket = _next(bucket, size, tz)
    return pd.DataFrame(rows, columns=columns)


def _floor_minute(dt: datetime) -> datetime:
    """``dt`` in UTC with seconds and microseconds dropped (naive input is rejected)."""
    if dt.tzinfo is None:
        raise ValueError("dt must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


def minute_trend(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo, minutes: int = 10) -> pd.DataFrame:
    """Columns ``minute`` (aware datetime in ``tz``), ``total``, ``ticketless``: ``minutes + 1`` zero-filled rows.

    The window is ``[floor_minute(now) - minutes, now)``, independent of the sidebar period.
    ``total`` = distinct (session, Track ID) detected in the minute; ``ticketless`` = distinct
    credentials whose check returned INVALID, WRONG_ROUTE or ALREADY_USED (the Invalid KPI set;
    unverified boardings are never counted). Minute buckets are formed on UTC minute keys (the
    fixed-width ``to_iso`` strings, ``substr(col, 1, 16)``) and only then converted to the display
    timezone, so a DST change can never merge or split a bucket.
    """
    last = _floor_minute(now)
    start = last - timedelta(minutes=minutes)
    params: Dict[str, object] = {"start": to_iso(start), "end": to_iso(now)}
    totals = conn.execute(
        "SELECT substr(detected_at, 1, 16) AS m, COUNT(DISTINCT session_id || ':' || track_id) "
        "FROM detection_records WHERE detected_at >= :start AND detected_at < :end GROUP BY m", params).fetchall()
    bad = {f"i{i}": o for i, o in enumerate(INVALID_OUTCOMES)}
    marks = ", ".join(f":{k}" for k in bad)
    failed = conn.execute(
        "SELECT substr(validated_at, 1, 16) AS m, COUNT(DISTINCT " + credential_key() + ") FROM validation_events "
        f"WHERE outcome IN ({marks}) AND validated_at >= :start AND validated_at < :end GROUP BY m",
        {**params, **bad}).fetchall()
    keys = [(start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M") for i in range(minutes + 1)]
    total = pd.Series({r[0]: int(r[1]) for r in totals}, dtype="int64").reindex(keys, fill_value=0)
    ticketless = pd.Series({r[0]: int(r[1]) for r in failed}, dtype="int64").reindex(keys, fill_value=0)
    minute = [datetime.strptime(k, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).astimezone(tz) for k in keys]
    return pd.DataFrame({"minute": minute, "total": total.astype("int64").to_list(),
                         "ticketless": ticketless.astype("int64").to_list()})


def outcome_distribution(conn: sqlite3.Connection, period: Period) -> pd.DataFrame:
    """``outcome, label, count, percent`` in OUTCOMES order (distinct credentials, as the KPI cards count)."""
    rows = conn.execute("SELECT outcome, COUNT(DISTINCT " + credential_key() + ") FROM validation_events WHERE "
                        + P("validated_at") + " GROUP BY outcome", period.params()).fetchall()
    found = {r[0]: int(r[1]) for r in rows if int(r[1])}
    total = sum(found.values())
    data = [(o, outcome_label(o), found[o], round(found[o] / total * 100, 1)) for o in OUTCOMES if o in found]
    return pd.DataFrame(data, columns=["outcome", "label", "count", "percent"])


def failure_reasons(conn: sqlite3.Connection, period: Period) -> pd.DataFrame:
    """Failed checks grouped by outcome and reason: ``outcome, label, reason, credentials, records``."""
    params = {**period.params(), **{f"f{i}": o for i, o in enumerate(FAILURE_OUTCOMES)}}
    marks = ", ".join(f":f{i}" for i in range(len(FAILURE_OUTCOMES)))
    rows = conn.execute(
        "SELECT outcome, reason, COUNT(DISTINCT " + credential_key() + ") AS credentials, COUNT(*) AS records "
        f"FROM validation_events WHERE outcome IN ({marks}) AND " + P("validated_at")
        + " GROUP BY outcome, reason ORDER BY records DESC, outcome, reason", params).fetchall()
    data = [(r[0], outcome_label(r[0]), r[1], int(r[2]), int(r[3])) for r in rows]
    return pd.DataFrame(data, columns=["outcome", "label", "reason", "credentials", "records"])
