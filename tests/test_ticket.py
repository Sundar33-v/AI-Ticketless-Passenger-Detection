"""Tests for QR reading, ticket validation and the demo ticket generator.

Run: python -m unittest discover -s tests -v   (pytest also works)
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from app.database.models import TicketStatus  # noqa: E402
from app.ticket import generate_test_tickets as gen  # noqa: E402
from app.ticket.qr_scanner import QRScanner, extract_ticket_id  # noqa: E402
from app.ticket.ticket_validator import (  # noqa: E402
    DEFAULT_TICKETS_PATH,
    TicketValidator,
    normalize_ticket_id,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def make_validator():
    return TicketValidator.from_dicts(gen.build_demo_tickets(now=NOW, valid_count=3))


def qr_frame(payload: str) -> np.ndarray:
    import qrcode

    qr = qrcode.QRCode(box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return np.array(img, dtype=np.uint8)[:, :, ::-1].copy()  # RGB -> BGR


class NormalizeTests(unittest.TestCase):
    def test_normalizes_case_and_whitespace(self):
        self.assertEqual(normalize_ticket_id("  tkt-0001\n"), "TKT-0001")
        self.assertEqual(normalize_ticket_id(b"TKT-0001"), "TKT-0001")

    def test_rejects_malformed(self):
        for bad in (None, "", "   ", "AB", "TKT 0001", "TKT-0001;DROP", "-TKT", "X" * 65, 123):
            self.assertIsNone(normalize_ticket_id(bad), bad)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.v = make_validator()

    def test_valid(self):
        r = self.v.validate("TKT-0001", now=NOW)
        self.assertEqual(r.status, TicketStatus.VALID)
        self.assertTrue(r.is_valid)
        self.assertEqual(r.ticket_id, "TKT-0001")

    def test_lowercase_input_is_valid(self):
        self.assertEqual(self.v.validate(" tkt-0002 ", now=NOW).status, TicketStatus.VALID)

    def test_not_found(self):
        r = self.v.validate("TKT-9999", now=NOW)
        self.assertEqual(r.status, TicketStatus.NOT_FOUND)
        self.assertEqual(r.ticket_id, "TKT-9999")

    def test_malformed_is_invalid(self):
        for bad in (None, "", "??", "not a ticket!"):
            r = self.v.validate(bad, now=NOW)
            self.assertEqual(r.status, TicketStatus.INVALID, bad)
            self.assertIsNone(r.ticket_id)

    def test_expired_future_cancelled_are_invalid(self):
        for tid, reason in (
            ("TKT-EXPIRED-01", "expired"),
            ("TKT-FUTURE-01", "not yet valid"),
            ("TKT-CANCEL-01", "cancelled"),
        ):
            r = self.v.validate(tid, now=NOW)
            self.assertEqual(r.status, TicketStatus.INVALID, tid)
            self.assertIn(reason, r.reason.lower())

    def test_used_in_json(self):
        self.assertEqual(self.v.validate("TKT-USED-01", now=NOW).status, TicketStatus.USED)

    def test_mark_used_consumes_ticket(self):
        self.assertEqual(self.v.validate("TKT-0001", now=NOW, mark_used=True).status, TicketStatus.VALID)
        self.assertEqual(self.v.validate("TKT-0001", now=NOW).status, TicketStatus.USED)
        self.assertEqual(self.v.validate("TKT-0002", now=NOW).status, TicketStatus.VALID)

    def test_validate_without_mark_used_is_repeatable(self):
        for _ in range(3):
            self.assertEqual(self.v.validate("TKT-0001", now=NOW).status, TicketStatus.VALID)

    def test_external_is_used_callback(self):
        r = self.v.validate("TKT-0001", now=NOW, is_used=lambda tid: tid == "TKT-0001")
        self.assertEqual(r.status, TicketStatus.USED)

    def test_validity_window_boundaries(self):
        v = TicketValidator.from_dicts([{
            "ticket_id": "TKT-W", "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2026-01-02T00:00:00Z",
        }])
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(v.validate("TKT-W", now=start).status, TicketStatus.VALID)
        self.assertEqual(v.validate("TKT-W", now=start + timedelta(days=1)).status, TicketStatus.VALID)
        self.assertEqual(v.validate("TKT-W", now=start - timedelta(seconds=1)).status, TicketStatus.INVALID)
        self.assertEqual(
            v.validate("TKT-W", now=start + timedelta(days=1, seconds=1)).status, TicketStatus.INVALID
        )
        # Naive "now" is treated as UTC.
        self.assertEqual(v.validate("TKT-W", now=datetime(2026, 1, 1, 12)).status, TicketStatus.VALID)

    def test_ticket_without_dates_is_valid(self):
        v = TicketValidator.from_dicts([{"ticket_id": "TKT-OPEN"}])
        self.assertEqual(v.validate("TKT-OPEN").status, TicketStatus.VALID)


class LoadingTests(unittest.TestCase):
    def test_load_from_file_and_list_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps([{"ticket_id": "abc-1"}]), encoding="utf-8")
            v = TicketValidator(path)
            self.assertEqual(len(v), 1)
            self.assertIn("ABC-1", v)

    def test_reload_picks_up_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            gen.write_tickets_json([{"ticket_id": "TKT-A"}], path)
            v = TicketValidator(path)
            self.assertEqual(v.validate("TKT-B").status, TicketStatus.NOT_FOUND)
            gen.write_tickets_json([{"ticket_id": "TKT-A"}, {"ticket_id": "TKT-B"}], path)
            v.reload()
            self.assertEqual(v.validate("TKT-B").status, TicketStatus.VALID)

    def test_bad_files_raise(self):
        cases = [
            [{"ticket_id": "TKT-1"}, {"ticket_id": "tkt-1"}],            # duplicate
            [{"ticket_id": "TKT-1", "status": "WHATEVER"}],              # unknown status
            [{"ticket_id": "bad id"}],                                   # malformed id
            [{"ticket_id": "TKT-1", "valid_until": "yesterday"}],        # bad timestamp
            [{"ticket_id": "TKT-1", "valid_from": "2026-02-01", "valid_until": "2026-01-01"}],
            {"no_tickets_key": []},
        ]
        for payload in cases:
            with self.assertRaises(ValueError, msg=payload):
                if isinstance(payload, list):
                    TicketValidator.from_dicts(payload)
                else:
                    from app.ticket.ticket_validator import parse_ticket_records
                    parse_ticket_records(payload)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            TicketValidator("does/not/exist.json")

    def test_repository_tickets_json_is_loadable(self):
        v = TicketValidator(DEFAULT_TICKETS_PATH)
        self.assertGreaterEqual(len(v), 4)
        self.assertEqual(v.validate("TKT-USED-01").status, TicketStatus.USED)
        self.assertEqual(v.validate("TKT-CANCEL-01").status, TicketStatus.INVALID)
        self.assertEqual(v.validate("TKT-DOES-NOT-EXIST").status, TicketStatus.NOT_FOUND)


class ExtractTicketIdTests(unittest.TestCase):
    def test_payload_formats(self):
        self.assertEqual(extract_ticket_id("TKT-0001"), "TKT-0001")
        self.assertEqual(extract_ticket_id(" ticket:TKT-0001 "), "TKT-0001")
        self.assertEqual(extract_ticket_id('{"ticket_id": "TKT-0001", "x": 1}'), "TKT-0001")
        self.assertEqual(extract_ticket_id(b"TKT-0001"), "TKT-0001")

    def test_unusable_payloads(self):
        self.assertIsNone(extract_ticket_id(None))
        self.assertIsNone(extract_ticket_id("   "))
        self.assertIsNone(extract_ticket_id("TICKET:"))
        self.assertIsNone(extract_ticket_id('{"other": 1}'))
        self.assertIsNone(extract_ticket_id("A" * 5000))
        self.assertEqual(extract_ticket_id("{not json"), "{not json")


class QRScannerTests(unittest.TestCase):
    def setUp(self):
        self.scanner = QRScanner(backend="opencv")

    def test_decode_all_payload_formats(self):
        for fmt in ("plain", "prefixed", "json"):
            dets = self.scanner.decode_frame(qr_frame(gen.qr_payload("TKT-0001", fmt)))
            self.assertEqual([d.ticket_id for d in dets], ["TKT-0001"], fmt)
            self.assertIsNotNone(dets[0].points)

    def test_grayscale_frame(self):
        gray = qr_frame("TKT-0002")[:, :, 0].copy()
        self.assertEqual(self.scanner.read_ticket_ids(gray), ["TKT-0002"])

    def test_blank_and_bad_frames(self):
        self.assertEqual(self.scanner.decode_frame(np.full((200, 200, 3), 255, np.uint8)), [])
        self.assertEqual(self.scanner.decode_frame(np.zeros((0, 0, 3), np.uint8)), [])
        self.assertEqual(self.scanner.decode_frame(None), [])

    def test_scan_and_validate(self):
        v = make_validator()
        frame = qr_frame("TKT-9999")
        [(det, res)] = self.scanner.scan_and_validate(frame, v, now=NOW)
        self.assertEqual(det.ticket_id, "TKT-9999")
        self.assertEqual(res.status, TicketStatus.NOT_FOUND)

    def test_invalid_backend(self):
        with self.assertRaises(ValueError):
            QRScanner(backend="face-id")


class GeneratorTests(unittest.TestCase):
    def test_demo_set_covers_every_ticket_status(self):
        v = make_validator()
        statuses = {v.validate(t["ticket_id"], now=NOW).status for t in gen.build_demo_tickets(now=NOW)}
        statuses.add(v.validate("TKT-UNKNOWN", now=NOW).status)
        self.assertEqual(statuses, set(TicketStatus))

    def test_cli_writes_json_and_scannable_qr_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_json, qr_dir = Path(tmp) / "tickets.json", Path(tmp) / "qr"
            rc = gen.main(["--output", str(out_json), "--qr-dir", str(qr_dir),
                           "--valid-count", "2", "--payload-format", "prefixed"])
            self.assertEqual(rc, 0)
            v = TicketValidator(out_json)
            self.assertEqual(len(v), 6)
            images = sorted(qr_dir.glob("*.png"))
            self.assertEqual(len(images), 6)
            scanner = QRScanner()
            dets = scanner.scan_image(qr_dir / "TKT-0001.png")
            self.assertEqual(dets[0].ticket_id, "TKT-0001")
            self.assertEqual(v.validate(dets[0].ticket_id).status, TicketStatus.VALID)

    def test_cli_no_qr(self):
        with tempfile.TemporaryDirectory() as tmp:
            qr_dir = Path(tmp) / "qr"
            gen.main(["--output", str(Path(tmp) / "t.json"), "--qr-dir", str(qr_dir), "--no-qr"])
            self.assertFalse(qr_dir.exists())

    def test_scan_image_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.png"
            bad.write_bytes(b"not an image")
            with self.assertRaises(ValueError):
                QRScanner().scan_image(bad)


if __name__ == "__main__":
    unittest.main()
