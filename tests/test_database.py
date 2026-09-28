"""Tests for the SQLite passenger event database.

Run: python -m unittest discover -s tests -v   (pytest also works)
"""

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.database import (  # noqa: E402
    Database,
    EventNotFoundError,
    PassengerEvent,
    PassengerRepository,
    PassengerStatus,
    TicketStatus,
    init_db,
)
from app.ticket.generate_test_tickets import build_demo_tickets  # noqa: E402
from app.ticket.ticket_validator import TicketValidator  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 28, 12, 0)  # naive UTC


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.db = init_db(":memory:")
        self.repo = PassengerRepository(self.db)
        self.validator = TicketValidator.from_dicts(build_demo_tickets(now=NOW, valid_count=3))

    def tearDown(self):
        self.db.dispose()

    def assign(self, event_id, ticket):
        return self.repo.validate_and_assign_ticket(event_id, ticket, self.validator, now=NOW)


class SchemaTests(RepoTestCase):
    def test_schema_columns(self):
        cols = {c["name"]: c for c in inspect(self.db.engine).get_columns("passenger_events")}
        for name in ("event_id", "track_id", "entry_time", "exit_time",
                     "ticket_id", "ticket_status", "passenger_status"):
            self.assertIn(name, cols)
        self.assertFalse(cols["track_id"]["nullable"])
        self.assertTrue(cols["exit_time"]["nullable"])
        self.assertTrue(cols["ticket_id"]["nullable"])

    def test_status_enums(self):
        self.assertEqual({s.value for s in TicketStatus}, {"VALID", "INVALID", "USED", "NOT_FOUND"})
        self.assertEqual({s.value for s in PassengerStatus}, {"PENDING", "VERIFIED", "NEEDS_CHECK"})

    def test_db_rejects_unknown_status_and_bad_exit(self):
        raw = self.db.engine.raw_connection()
        try:
            cur = raw.cursor()
            with self.assertRaises(sqlite3.IntegrityError):
                cur.execute(
                    "INSERT INTO passenger_events (track_id, entry_time, passenger_status, created_at, updated_at)"
                    " VALUES (1, '2026-01-01', 'TICKETLESS', '2026-01-01', '2026-01-01')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                cur.execute(
                    "INSERT INTO passenger_events (track_id, entry_time, exit_time, passenger_status, created_at, updated_at)"
                    " VALUES (1, '2026-01-02', '2026-01-01', 'PENDING', '2026-01-01', '2026-01-01')"
                )
        finally:
            raw.rollback()
            raw.close()

    def test_db_enforces_single_valid_use_per_ticket(self):
        with self.assertRaises(IntegrityError):
            with self.db.session() as s:
                for track in (1, 2):
                    s.add(PassengerEvent(track_id=track, entry_time=T0, ticket_id="TKT-0001",
                                         ticket_status=TicketStatus.VALID,
                                         passenger_status=PassengerStatus.VERIFIED))
        # Non-VALID duplicates are allowed (e.g. two failed scans of the same ID).
        with self.db.session() as s:
            for track in (1, 2):
                s.add(PassengerEvent(track_id=track, entry_time=T0, ticket_id="TKT-0001",
                                     ticket_status=TicketStatus.USED,
                                     passenger_status=PassengerStatus.NEEDS_CHECK))


class EntryExitTests(RepoTestCase):
    def test_new_entry_is_pending_without_ticket(self):
        ev = self.repo.create_entry(track_id=7, entry_time=T0)
        self.assertIsNotNone(ev.event_id)
        self.assertEqual(ev.track_id, 7)
        self.assertEqual(ev.passenger_status, PassengerStatus.PENDING)
        self.assertIsNone(ev.ticket_id)
        self.assertIsNone(ev.ticket_status)
        self.assertIsNone(ev.exit_time)

    def test_default_entry_time_and_tz_aware_input(self):
        ev = self.repo.create_entry(1)
        self.assertIsNotNone(ev.entry_time)
        ist = timezone(timedelta(hours=5, minutes=30))
        ev2 = self.repo.create_entry(2, entry_time=datetime(2026, 9, 28, 17, 30, tzinfo=ist))
        self.assertEqual(self.repo.get_event(ev2.event_id).entry_time, T0)

    def test_invalid_track_id(self):
        for bad in (-1, "3", None, True):
            with self.assertRaises(ValueError):
                self.repo.create_entry(bad)

    def test_exit_without_ticket_needs_check_not_ticketless(self):
        ev = self.repo.create_entry(1, entry_time=T0)
        ev = self.repo.record_exit(ev.event_id, exit_time=T0 + timedelta(minutes=10))
        self.assertEqual(ev.exit_time, T0 + timedelta(minutes=10))
        self.assertEqual(ev.passenger_status, PassengerStatus.NEEDS_CHECK)
        self.assertIsNone(ev.ticket_status)

    def test_exit_after_verification_stays_verified(self):
        ev = self.repo.create_entry(1, entry_time=T0)
        self.assign(ev.event_id, "TKT-0001")
        ev = self.repo.record_exit(ev.event_id, exit_time=T0 + timedelta(minutes=1))
        self.assertEqual(ev.passenger_status, PassengerStatus.VERIFIED)

    def test_exit_is_idempotent_and_validated(self):
        ev = self.repo.create_entry(1, entry_time=T0)
        with self.assertRaises(ValueError):
            self.repo.record_exit(ev.event_id, exit_time=T0 - timedelta(seconds=1))
        first = self.repo.record_exit(ev.event_id, exit_time=T0 + timedelta(minutes=1))
        second = self.repo.record_exit(ev.event_id, exit_time=T0 + timedelta(minutes=5))
        self.assertEqual(first.exit_time, second.exit_time)

    def test_missing_event(self):
        with self.assertRaises(EventNotFoundError):
            self.repo.record_exit(999)
        with self.assertRaises(EventNotFoundError):
            self.assign(999, "TKT-0001")
        self.assertIsNone(self.repo.get_event(999))

    def test_open_event_for_track(self):
        a = self.repo.create_entry(5, entry_time=T0)
        self.repo.record_exit(a.event_id, exit_time=T0 + timedelta(minutes=1))
        self.assertIsNone(self.repo.get_open_event_for_track(5))
        b = self.repo.create_entry(5, entry_time=T0 + timedelta(minutes=2))
        self.assertEqual(self.repo.get_open_event_for_track(5).event_id, b.event_id)
        self.assertIsNone(self.repo.get_open_event_for_track(6))


class TicketAssignmentTests(RepoTestCase):
    def test_valid_ticket_verifies(self):
        ev = self.repo.create_entry(1, entry_time=T0)
        ev, res = self.assign(ev.event_id, " tkt-0001 ")
        self.assertEqual(res.status, TicketStatus.VALID)
        self.assertEqual((ev.ticket_id, ev.ticket_status, ev.passenger_status),
                         ("TKT-0001", TicketStatus.VALID, PassengerStatus.VERIFIED))

    def test_failed_scans_need_check(self):
        expected = {
            "TKT-9999": TicketStatus.NOT_FOUND,
            "TKT-EXPIRED-01": TicketStatus.INVALID,
            "TKT-CANCEL-01": TicketStatus.INVALID,
            "TKT-USED-01": TicketStatus.USED,
            "garbage!!": TicketStatus.INVALID,
        }
        for i, (tid, status) in enumerate(expected.items()):
            ev = self.repo.create_entry(i, entry_time=T0)
            ev, res = self.assign(ev.event_id, tid)
            self.assertEqual(res.status, status, tid)
            self.assertEqual(ev.ticket_status, status, tid)
            self.assertEqual(ev.passenger_status, PassengerStatus.NEEDS_CHECK, tid)
            self.assertIsNotNone(ev.ticket_id)

    def test_ticket_reuse_by_another_passenger_is_used(self):
        a = self.repo.create_entry(1, entry_time=T0)
        b = self.repo.create_entry(2, entry_time=T0)
        self.assign(a.event_id, "TKT-0001")
        ev_b, res = self.assign(b.event_id, "TKT-0001")
        self.assertEqual(res.status, TicketStatus.USED)
        self.assertEqual(ev_b.passenger_status, PassengerStatus.NEEDS_CHECK)
        self.assertTrue(self.repo.is_ticket_in_use("TKT-0001"))
        self.assertFalse(self.repo.is_ticket_in_use("TKT-0001", exclude_event_id=a.event_id))

    def test_rescan_by_same_passenger_stays_valid(self):
        a = self.repo.create_entry(1, entry_time=T0)
        self.assign(a.event_id, "TKT-0001")
        ev, res = self.assign(a.event_id, "TKT-0001")
        self.assertEqual(res.status, TicketStatus.VALID)
        self.assertEqual(ev.passenger_status, PassengerStatus.VERIFIED)

    def test_verified_not_downgraded_by_bad_rescan(self):
        a = self.repo.create_entry(1, entry_time=T0)
        self.assign(a.event_id, "TKT-0001")
        ev, res = self.assign(a.event_id, "TKT-9999")
        self.assertEqual(res.status, TicketStatus.NOT_FOUND)
        self.assertEqual((ev.ticket_id, ev.ticket_status, ev.passenger_status),
                         ("TKT-0001", TicketStatus.VALID, PassengerStatus.VERIFIED))

    def test_needs_check_can_recover_with_valid_ticket(self):
        a = self.repo.create_entry(1, entry_time=T0)
        self.assign(a.event_id, "TKT-9999")
        ev, _ = self.assign(a.event_id, "TKT-0002")
        self.assertEqual(ev.passenger_status, PassengerStatus.VERIFIED)

    def test_manual_status_override(self):
        a = self.repo.create_entry(1, entry_time=T0)
        self.assertEqual(self.repo.mark_needs_check(a.event_id).passenger_status, PassengerStatus.NEEDS_CHECK)
        ev = self.repo.set_passenger_status(a.event_id, "VERIFIED")
        self.assertEqual(ev.passenger_status, PassengerStatus.VERIFIED)
        with self.assertRaises(ValueError):
            self.repo.set_passenger_status(a.event_id, "TICKETLESS")


class QueryTests(RepoTestCase):
    def test_list_and_counts(self):
        a = self.repo.create_entry(1, entry_time=T0)
        b = self.repo.create_entry(2, entry_time=T0 + timedelta(seconds=1))
        self.repo.create_entry(3, entry_time=T0 + timedelta(seconds=2))
        self.assign(a.event_id, "TKT-0001")
        self.assign(b.event_id, "TKT-9999")

        self.assertEqual(self.repo.status_counts(), {"PENDING": 1, "VERIFIED": 1, "NEEDS_CHECK": 1})
        self.assertEqual([e.track_id for e in self.repo.list_events()], [3, 2, 1])
        self.assertEqual([e.track_id for e in self.repo.list_events(PassengerStatus.VERIFIED)], [1])
        self.assertEqual([e.track_id for e in self.repo.list_events(ticket_status="NOT_FOUND")], [2])
        self.assertEqual(len(self.repo.list_events(limit=1)), 1)
        self.assertEqual([e.track_id for e in self.repo.list_events(limit=1, offset=1)], [2])

    def test_to_dict(self):
        a = self.repo.create_entry(1, entry_time=T0)
        ev, _ = self.assign(a.event_id, "TKT-0001")
        d = ev.to_dict()
        self.assertEqual(d["ticket_status"], "VALID")
        self.assertEqual(d["passenger_status"], "VERIFIED")
        self.assertEqual(d["entry_time"], T0.isoformat())
        self.assertIsNone(d["exit_time"])


class FileDatabaseTests(unittest.TestCase):
    def test_persists_across_instances_and_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "passengers.db"
            db = init_db(path)
            repo = PassengerRepository(db)
            errors = []

            def worker(offset):
                try:
                    for i in range(10):
                        repo.create_entry(offset + i, entry_time=T0)
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(n * 100,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            db.dispose()
            self.assertEqual(errors, [])
            self.assertTrue(path.exists())

            db2 = Database(path)
            try:
                self.assertEqual(PassengerRepository(db2).status_counts()["PENDING"], 40)
            finally:
                db2.dispose()


if __name__ == "__main__":
    unittest.main()
