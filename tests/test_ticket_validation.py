"""Tests for app/ticket_validator.py: rules, onward travel, persistence, precedence, concurrency."""

from __future__ import annotations

import hashlib
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.database import to_iso
from app.qr_service import encode_payload, hash_token
from app.settings_schema import AppConfig
from app.ticket_validator import (
    JourneyContext,
    Outcome,
    TicketRecord,
    TicketValidator,
    build_journey,
    cancel_ticket,
    evaluate_ticket,
    get_ticket,
    issue_ticket,
    normalize_ticket_id,
    rotate_token,
)

IST = ZoneInfo("Asia/Kolkata")
D = date(2026, 1, 15)
TRIP = "R1-outbound-2026-01-15"


def local(hour, minute=0, day=D):
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


NOW = local(11)


def record(b="Market Road", d="Railway Station", *, status="ACTIVE", route="R1", valid_from=None,
           expires_at=None, used_trip_id=None, used_boarding_id=None):
    return TicketRecord(
        ticket_id="TKT-1", passenger_ref="PSG-0001", route_id=route, boarding_station=b, destination_station=d,
        travel_date="2026-01-15", valid_from=(valid_from or local(5)).astimezone(timezone.utc),
        expires_at=(expires_at or local(23)).astimezone(timezone.utc), status=status,
        used_at=local(9).astimezone(timezone.utc) if status == "USED" else None,
        used_trip_id=used_trip_id, used_boarding_id=used_boarding_id)


def journey_at(cfg, station="City Hospital", **kw):
    return build_journey(cfg, now=NOW, station=station, **kw)


def ev(ticket, journey, now=NOW, *, present=True, wellformed=True, boarding_id=None, ctype="qr_token"):
    return evaluate_ticket(ticket, journey, now, credential_present=present, credential_wellformed=wellformed,
                           boarding_id=boarding_id, tz=IST, credential_type=ctype)


def issue(conn, cfg, b="Central Bus Stand", d="Airport", tid=None, **kw):
    kw.setdefault("valid_from", local(5))
    kw.setdefault("expires_at", local(23))
    return issue_ticket(conn, route=cfg.route, tz=IST, boarding_station=b, destination_station=d,
                        travel_date=D, ticket_id=tid, now=NOW, **kw)


def boarding(conn, track, sid="s1", status=None):
    stamp = to_iso(NOW)
    conn.execute("INSERT OR IGNORE INTO detection_sessions (session_id, source_type, source_label, status, "
                 "started_at, last_heartbeat_at) VALUES (?, 'video', 'clip.mp4', 'RUNNING', ?, ?)",
                 (sid, stamp, stamp))
    cur = conn.execute("INSERT INTO boarding_events (session_id, track_id, boarded_at, direction, trip_id) "
                       "VALUES (?, ?, ?, 'top_to_bottom', ?)", (sid, track, stamp, TRIP))
    if status:
        conn.execute("UPDATE boarding_events SET verification_status = ? WHERE boarding_id = ?",
                     (status, cur.lastrowid))
    return cur.lastrowid


@pytest.fixture
def conn(db):
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def validator():
    return TicketValidator(tz=IST, clock=lambda: NOW)


def check(validator, conn, journey, credential, ctype="qr_token", **kw):
    return validator.validate(conn, credential, credential_type=ctype, journey=journey,
                              channel=kw.pop("channel", "dashboard"), **kw)


# ------------------------------------------------------------ normalize
def test_normalize_ticket_id_case_and_whitespace():
    assert normalize_ticket_id("  tkt-0001\n") == "TKT-0001"
    assert normalize_ticket_id(b"TKT-0001") == "TKT-0001"


@pytest.mark.parametrize("bad", [None, "", "   ", "AB", "TKT 0001", "TKT-0001;DROP", "-TKT", "X" * 65, 123])
def test_normalize_ticket_id_rejects_malformed(bad):
    assert normalize_ticket_id(bad) is None


# ----------------------------------------------------- pure rules (AC 9)
def test_rule_order_no_credential_and_malformed(cfg):
    j = journey_at(cfg)
    assert ev(None, j, present=False).outcome is Outcome.VERIFICATION_REQUIRED
    assert ev(None, j, wellformed=False).reason == "Unrecognised QR code"
    assert ev(None, j, wellformed=False, ctype="ticket_id").reason == "Malformed ticket ID"
    assert ev(None, j).outcome is Outcome.INVALID


def test_cancelled_and_window_rules(cfg):
    j = journey_at(cfg)
    assert ev(record(status="CANCELLED"), j).reason == "Ticket cancelled"
    assert ev(record(), j, now=local(23)).outcome is Outcome.EXPIRED  # now == expires_at
    assert ev(record(), j, now=local(22, 59)).outcome is Outcome.VALID
    early = ev(record(), j, now=local(4, 59))
    assert early.outcome is Outcome.INVALID and "not valid yet" in early.reason


def test_overnight_window_valid_after_midnight(cfg):
    ticket = record(valid_from=local(20), expires_at=local(2, day=D + timedelta(days=1)))
    j = journey_at(cfg)
    assert ev(ticket, j, now=local(0, 30, day=D + timedelta(days=1))).outcome is Outcome.VALID


@pytest.mark.parametrize("station, outcome, reason", [
    ("City Hospital", Outcome.VALID, "Valid for Market Road → Railway Station"),  # intermediate stop
    ("College Gate", Outcome.VALID, "Valid for"),
    ("Market Road", Outcome.VALID, "Valid for"),                                   # boarding station
    ("Railway Station", Outcome.VALID, "At destination (alighting)"),
    ("Airport", Outcome.WRONG_ROUTE, "beyond the ticket destination"),
    ("Central Bus Stand", Outcome.WRONG_ROUTE, "not reached the ticket's boarding station"),
])
def test_onward_travel(cfg, station, outcome, reason):
    decision = ev(record(), journey_at(cfg, station))
    assert decision.outcome is outcome and reason in decision.reason


def test_opposite_direction_and_other_route(cfg):
    j = journey_at(cfg)
    assert "opposite direction" in ev(record("Railway Station", "Market Road"), j).reason
    assert ev(record(route="R9"), j).reason == "Ticket is for route R9"
    assert ev(record("Nowhere", "Airport"), j).reason == "Station not on this route"


def test_inbound_journey_reverses_order():
    cfg = AppConfig.from_dict({"journey": {"direction": "inbound"}})
    j = build_journey(cfg, now=NOW, station="College Gate")
    assert j.ordered_stations[0] == "Airport"
    assert ev(record("Railway Station", "Market Road"), j).outcome is Outcome.VALID
    assert ev(record("Market Road", "Railway Station"), j).outcome is Outcome.WRONG_ROUTE


def test_expired_at_intermediate_station_is_expired(cfg):
    j = journey_at(cfg, "City Hospital")
    assert ev(record(), j, now=local(23, 30)).outcome is Outcome.EXPIRED


def test_expired_beats_wrong_route_and_already_used(cfg):
    j = journey_at(cfg, "Airport")
    assert ev(record(status="USED", used_trip_id="other"), j, now=local(23, 30)).outcome is Outcome.EXPIRED


def test_no_current_station_skips_positional_checks():
    cfg = AppConfig.from_dict({"journey": {"current_station": None}})
    j = build_journey(cfg, now=NOW)
    assert j.current_station is None
    assert ev(record("Central Bus Stand", "Market Road"), j).outcome is Outcome.VALID


def test_used_ticket_rules(cfg):
    j = journey_at(cfg)
    used = record(status="USED", used_trip_id=TRIP, used_boarding_id=1)
    assert ev(used, j, boarding_id=2).outcome is Outcome.ALREADY_USED
    same = ev(used, j, boarding_id=1)
    assert same.outcome is Outcome.VALID and same.reason.startswith("Re-check") and not same.bind
    assert ev(used, j).outcome is Outcome.VALID  # desk re-check without a boarding
    other_trip = record(status="USED", used_trip_id="R1-outbound-2026-01-14", used_boarding_id=1)
    assert ev(other_trip, j, boarding_id=1).outcome is Outcome.ALREADY_USED
    unbound = record(status="USED", used_trip_id=TRIP)
    assert ev(unbound, j, boarding_id=5).bind


# ------------------------------------------------------------ build_journey
def test_build_journey_trip_ids(cfg):
    assert build_journey(cfg, now=NOW).trip_id == TRIP
    late = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)  # 01:30 IST on the 16th
    assert build_journey(cfg, now=late).trip_id == "R1-outbound-2026-01-16"
    assert build_journey(cfg, now=NOW, trip_id="EXPLICIT").trip_id == "EXPLICIT"
    fixed = AppConfig.from_dict({"journey": {"trip_id": "FIXED"}})
    assert build_journey(fixed, now=NOW).trip_id == "FIXED"
    assert build_journey(fixed, now=NOW, trip_id="R1-outbound-2026-01-14").trip_id == "R1-outbound-2026-01-14"


def test_build_journey_errors_and_defaults(cfg):
    with pytest.raises(ValueError, match="not on route R1"):
        build_journey(cfg, now=NOW, station="Mars")
    with pytest.raises(ValueError):
        build_journey(cfg, now=datetime(2026, 1, 15, 11))
    j = build_journey(cfg, now=NOW)
    assert isinstance(j, JourneyContext) and j.current_station == "City Hospital"
    assert j.route_id == "R1" and j.direction == "outbound"


def test_pipeline_and_dashboard_trip_id_parity(cfg):
    pipeline = build_journey(cfg, now=local(6, 30), station=None, trip_id=None)
    dashboard = build_journey(cfg, now=local(21, 0), station="Airport", trip_id=None)
    assert pipeline.trip_id == dashboard.trip_id


# ------------------------------------------------------------- issue/cancel
def test_issue_ticket_defaults_never_born_expired(conn, cfg):
    at_14 = local(14)
    ticket, token = issue_ticket(conn, route=cfg.route, tz=IST, boarding_station="Market Road",
                                 destination_station="Airport", travel_date=D, now=at_14)
    assert ticket.valid_from == at_14.astimezone(timezone.utc)
    assert ticket.expires_at == ticket.valid_from + timedelta(hours=12)
    assert ticket.ticket_id.startswith("TKT-") and ticket.passenger_ref.startswith("PSG-")
    assert len(token) == 32 and ticket.qr_token_hash == hash_token(token)
    j = build_journey(cfg, now=local(14, 1))
    assert ev(ticket, j, now=local(14, 1)).outcome is Outcome.VALID
    row = conn.execute("SELECT * FROM tickets").fetchone()
    assert token not in [str(v) for v in tuple(row)]  # plaintext token is never stored


def test_issue_ticket_future_date_starts_at_local_midnight(conn, cfg):
    ticket, _ = issue_ticket(conn, route=cfg.route, tz=IST, boarding_station="Market Road",
                             destination_station="Airport", travel_date=D + timedelta(days=1), now=NOW)
    assert ticket.valid_from == local(0, day=D + timedelta(days=1)).astimezone(timezone.utc)


@pytest.mark.parametrize("kwargs", [
    {"valid_from": datetime(2026, 1, 15, 5)},                       # naive
    {"valid_from": local(10), "expires_at": local(9)},             # inverted
    {"valid_from": local(10, day=D + timedelta(days=1))},           # not on travel_date
    {"valid_hours": 0}, {"valid_hours": 73},
    {"b": "Mars"},
])
def test_issue_ticket_rejects_bad_input(conn, cfg, kwargs):
    b = kwargs.pop("b", "Market Road")
    kwargs.setdefault("now", NOW)
    with pytest.raises(ValueError):
        issue_ticket(conn, route=cfg.route, tz=IST, boarding_station=b, destination_station="Airport",
                     travel_date=D, **kwargs)


def test_issue_ticket_duplicate_id_raises(conn, cfg):
    import sqlite3

    issue(conn, cfg, tid="DEMO-TKT-001")
    with pytest.raises(sqlite3.IntegrityError):
        issue(conn, cfg, tid="DEMO-TKT-001")


def test_cancel_ticket_branches(conn, cfg, validator, journey):
    with pytest.raises(LookupError):
        cancel_ticket(conn, "TKT-NOPE", now=NOW)
    with pytest.raises(ValueError):
        cancel_ticket(conn, "bad id!", now=NOW)
    ticket, token = issue(conn, cfg, tid="TKT-C1")
    assert cancel_ticket(conn, "tkt-c1", now=NOW).status == "CANCELLED"
    assert cancel_ticket(conn, "TKT-C1", now=NOW).status == "CANCELLED"  # no-op
    result = check(validator, conn, journey, encode_payload(token))
    assert result.outcome is Outcome.INVALID and result.reason == "Ticket cancelled"
    _, token2 = issue(conn, cfg, tid="TKT-C2")
    assert check(validator, conn, journey, encode_payload(token2)).outcome is Outcome.VALID
    with pytest.raises(ValueError, match="used ticket"):
        cancel_ticket(conn, "TKT-C2", now=NOW)


def test_rotate_token_invalidates_old(conn, cfg, validator, journey):
    _, old = issue(conn, cfg, tid="TKT-R1")
    new = rotate_token(conn, "TKT-R1", now=NOW)
    assert new != old
    assert check(validator, conn, journey, old).outcome is Outcome.INVALID
    assert check(validator, conn, journey, encode_payload(new)).outcome is Outcome.VALID
    with pytest.raises(LookupError):
        rotate_token(conn, "TKT-NONE")


def test_get_ticket_requires_one_key(conn):
    with pytest.raises(ValueError):
        get_ticket(conn)
    assert get_ticket(conn, ticket_id="bad id!") is None


# -------------------------------------------------------------- validate()
def test_validate_persists_and_binds(conn, cfg, validator, journey):
    ticket, token = issue(conn, cfg, tid="TKT-V1")
    bid = boarding(conn, 3)
    result = check(validator, conn, journey, encode_payload(token), boarding_id=bid, session_id="s1",
                   channel="camera")
    assert result.outcome is Outcome.VALID and result.ticket_id == "TKT-V1"
    assert result.passenger_ref == ticket.passenger_ref and result.boarding_id == bid
    t = get_ticket(conn, ticket_id="TKT-V1")
    assert (t.status, t.used_trip_id, t.used_boarding_id) == ("USED", TRIP, bid)
    row = conn.execute("SELECT * FROM validation_events WHERE validation_id = ?", (result.validation_id,)).fetchone()
    assert row["outcome"] == "VALID" and row["token_hash"] == hash_token(token)
    assert (row["channel"], row["station"], row["trip_id"], row["session_id"]) == ("camera", "City Hospital",
                                                                                   TRIP, "s1")
    b = conn.execute("SELECT * FROM boarding_events WHERE boarding_id = ?", (bid,)).fetchone()
    assert (b["verification_status"], b["ticket_id"], b["passenger_ref"], b["last_validation_id"]) == (
        "VALID", "TKT-V1", ticket.passenger_ref, result.validation_id)


def test_second_boarding_same_trip_already_used(conn, cfg, validator, journey):
    _, token = issue(conn, cfg, tid="TKT-U1")
    b1, b2 = boarding(conn, 1), boarding(conn, 2)
    assert check(validator, conn, journey, token, boarding_id=b1).outcome is Outcome.VALID
    assert check(validator, conn, journey, token, boarding_id=b2).outcome is Outcome.ALREADY_USED
    assert check(validator, conn, journey, token, boarding_id=b1).outcome is Outcome.VALID  # re-check
    assert check(validator, conn, journey, token).outcome is Outcome.VALID  # desk re-check
    other = build_journey(cfg, now=NOW, trip_id="R1-outbound-2026-01-16")
    assert check(validator, conn, other, token).outcome is Outcome.ALREADY_USED


def test_desk_check_then_boarding_binds_once(conn, cfg, validator, journey):
    _, token = issue(conn, cfg, tid="TKT-D1")
    assert check(validator, conn, journey, token).outcome is Outcome.VALID
    assert get_ticket(conn, ticket_id="TKT-D1").used_boarding_id is None
    bid = boarding(conn, 4)
    assert check(validator, conn, journey, token, boarding_id=bid).outcome is Outcome.VALID
    assert get_ticket(conn, ticket_id="TKT-D1").used_boarding_id == bid


def test_unknown_and_malformed_credentials(conn, validator, journey):
    unknown = check(validator, conn, journey, encode_payload("A" * 32))
    assert unknown.outcome is Outcome.INVALID and "No ticket record" in unknown.reason
    missing = check(validator, conn, journey, "TKT-9999", "ticket_id")
    assert missing.outcome is Outcome.INVALID
    row = conn.execute("SELECT presented_ref, ticket_id FROM validation_events WHERE validation_id = ?",
                       (missing.validation_id,)).fetchone()
    assert (row["presented_ref"], row["ticket_id"]) == ("TKT-9999", None)


def test_malformed_ticket_id_hash_n_f(conn, validator, journey):
    raw = "  bad id!  "
    result = check(validator, conn, journey, raw, "ticket_id")
    assert result.outcome is Outcome.INVALID and result.reason == "Malformed ticket ID"
    row = conn.execute("SELECT * FROM validation_events WHERE validation_id = ?", (result.validation_id,)).fetchone()
    assert row["presented_ref"] is None
    assert row["token_hash"] == hashlib.sha256("BAD ID!".encode()).hexdigest()
    ok = check(validator, conn, journey, " tkt-0042 ", "ticket_id")
    row = conn.execute("SELECT * FROM validation_events WHERE validation_id = ?", (ok.validation_id,)).fetchone()
    assert row["token_hash"] == hash_token("TKT-0042") and row["presented_ref"] == "TKT-0042"


def test_json_qr_payload_is_unrecognised_and_not_stored(conn, cfg, validator, journey):
    issue(conn, cfg, tid="TKT-J1")
    payload = '{"ticket_id": "TKT-J1", "name": "Sample Person"}'
    result = check(validator, conn, journey, payload)
    assert result.outcome is Outcome.INVALID and result.reason == "Unrecognised QR code"
    row = conn.execute("SELECT * FROM validation_events").fetchone()
    assert all("Sample Person" not in str(v) for v in tuple(row))
    assert row["ticket_id"] is None


def test_no_credential_records_verification_required(conn, validator, journey):
    bid = boarding(conn, 9)
    result = check(validator, conn, journey, None, "none", boarding_id=bid)
    assert result.outcome is Outcome.VERIFICATION_REQUIRED
    row = conn.execute("SELECT credential_type, token_hash FROM validation_events").fetchone()
    assert tuple(row) == ("none", None)


def test_unknown_boarding_raises_and_writes_nothing(conn, validator, journey):
    with pytest.raises(ValueError, match="Unknown boarding event"):
        check(validator, conn, journey, "TKT-0001", "ticket_id", boarding_id=999)
    assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 0


def test_validate_refuses_open_transaction(conn, validator, journey):
    from app.database import transaction

    with transaction(conn):
        with pytest.raises(RuntimeError):
            check(validator, conn, journey, None, "none")


def test_event_key_idempotency(conn, cfg, validator, journey):
    _, token = issue(conn, cfg, tid="TKT-E1")
    first = check(validator, conn, journey, token, event_key="demo:s:1", channel="demo", is_synthetic=True)
    again = check(validator, conn, journey, "garbage", event_key="demo:s:1", channel="demo", is_synthetic=True)
    assert again == first
    assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 1
    assert conn.execute("SELECT is_synthetic FROM validation_events").fetchone()[0] == 1


# ------------------------------------------------------- precedence (M3)
def test_valid_boarding_never_downgraded(conn, cfg, validator, journey):
    _, token = issue(conn, cfg, tid="TKT-N1")
    bid = boarding(conn, 1)
    check(validator, conn, journey, token, boarding_id=bid)
    bad = check(validator, conn, journey, "TKT-NONE", "ticket_id", boarding_id=bid)
    assert bad.outcome is Outcome.INVALID
    b = conn.execute("SELECT verification_status, ticket_id, last_validation_id FROM boarding_events").fetchone()
    assert (b[0], b[1], b[2]) == ("VALID", "TKT-N1", bad.validation_id)
    assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 2


def test_m3_expired_boarding_resolved_by_valid_check(conn, cfg, validator, journey):
    _, expired = issue(conn, cfg, tid="TKT-X1", valid_from=local(1), expires_at=local(6))
    _, good = issue(conn, cfg, tid="TKT-X2")
    bid = boarding(conn, 1)
    assert check(validator, conn, journey, expired, boarding_id=bid).outcome is Outcome.EXPIRED
    assert conn.execute("SELECT verification_status FROM boarding_events").fetchone()[0] == "EXPIRED"
    assert check(validator, conn, journey, good, boarding_id=bid).outcome is Outcome.VALID
    b = conn.execute("SELECT verification_status, ticket_id, passenger_ref FROM boarding_events").fetchone()
    assert b[0] == "VALID" and b[1] == "TKT-X2" and b[2] is not None


def test_m3_no_ticket_record_keeps_failed_status(conn, cfg, validator, journey):
    _, expired = issue(conn, cfg, tid="TKT-X3", valid_from=local(1), expires_at=local(6))
    bid = boarding(conn, 1)
    check(validator, conn, journey, expired, boarding_id=bid)
    none = check(validator, conn, journey, None, "none", boarding_id=bid)
    b = conn.execute("SELECT verification_status, last_validation_id FROM boarding_events").fetchone()
    assert (b[0], b[1]) == ("EXPIRED", none.validation_id)


def test_m3_valid_boarding_invalid_check_stays_valid(conn, cfg, validator, journey):
    _, token = issue(conn, cfg, tid="TKT-X4")
    bid = boarding(conn, 1)
    check(validator, conn, journey, token, boarding_id=bid)
    check(validator, conn, journey, encode_payload("Z" * 40), boarding_id=bid)
    assert conn.execute("SELECT verification_status FROM boarding_events").fetchone()[0] == "VALID"
    assert conn.execute("SELECT COUNT(*) FROM validation_events WHERE outcome='INVALID'").fetchone()[0] == 1


def test_failed_check_overwrites_non_valid_status(conn, validator, journey):
    bid = boarding(conn, 1)
    check(validator, conn, journey, "TKT-NONE", "ticket_id", boarding_id=bid)
    assert conn.execute("SELECT verification_status FROM boarding_events").fetchone()[0] == "INVALID"


# ------------------------------------------------------- concurrency (AC 10)
def test_two_threads_same_ticket_one_valid_one_already_used(db, cfg, journey):
    setup = db.connect()
    try:
        _, token = issue(setup, cfg, tid="TKT-CC1")
        boardings = [boarding(setup, 1), boarding(setup, 2)]
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(bid):
        conn = db.connect()
        try:
            barrier.wait(timeout=10)
            results.append(TicketValidator(tz=IST, clock=lambda: NOW).validate(
                conn, encode_payload(token), credential_type="qr_token", journey=journey,
                channel="camera", boarding_id=bid))
        except Exception as exc:  # surfaced below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(bid,)) for bid in boardings]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert sorted(r.outcome.value for r in results) == ["ALREADY_USED", "VALID"]


def test_reason_constants_match_the_rule_texts(cfg):
    from app.ticket_validator import MALFORMED_TICKET_REASON, NO_RECORD_QR_REASON, UNRECOGNISED_QR_REASON

    j = journey_at(cfg)
    assert ev(None, j, ctype="qr_token").reason == NO_RECORD_QR_REASON
    assert ev(None, j, wellformed=False, ctype="qr_token").reason == UNRECOGNISED_QR_REASON
    assert ev(None, j, wellformed=False, ctype="ticket_id").reason == MALFORMED_TICKET_REASON
