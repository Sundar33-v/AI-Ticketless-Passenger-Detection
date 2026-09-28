"""Generate demo tickets (tickets.json) and matching QR code PNGs.

    python -m app.ticket.generate_test_tickets                   # data/tickets.json + data/qr_codes/
    python -m app.ticket.generate_test_tickets --no-qr
    python -m app.ticket.generate_test_tickets --valid-count 10 --qr-dir out/qr

Demo set: N ACTIVE tickets valid for 365 days, plus one each of expired,
not-yet-valid, USED and CANCELLED - covering every validation outcome.
These are test fixtures only; they contain no personal data.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from app.ticket.ticket_validator import DEFAULT_TICKETS_PATH, PROJECT_ROOT

DEFAULT_QR_DIR = PROJECT_ROOT / "data" / "qr_codes"


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def build_demo_tickets(now: Optional[datetime] = None, valid_count: int = 5) -> List[Dict[str, str]]:
    if valid_count < 0:
        raise ValueError("valid_count must be >= 0")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = timedelta(days=1)

    tickets = [
        {
            "ticket_id": f"TKT-{i:04d}",
            "status": "ACTIVE",
            "valid_from": _iso(now - day),
            "valid_until": _iso(now + 365 * day),
            "ticket_type": "SINGLE",
        }
        for i in range(1, valid_count + 1)
    ]
    tickets += [
        {"ticket_id": "TKT-EXPIRED-01", "status": "ACTIVE",
         "valid_from": _iso(now - 30 * day), "valid_until": _iso(now - 2 * day), "ticket_type": "SINGLE"},
        {"ticket_id": "TKT-FUTURE-01", "status": "ACTIVE",
         "valid_from": _iso(now + 30 * day), "valid_until": _iso(now + 60 * day), "ticket_type": "SINGLE"},
        {"ticket_id": "TKT-USED-01", "status": "USED",
         "valid_from": _iso(now - day), "valid_until": _iso(now + 365 * day), "ticket_type": "SINGLE"},
        {"ticket_id": "TKT-CANCEL-01", "status": "CANCELLED",
         "valid_from": _iso(now - day), "valid_until": _iso(now + 365 * day), "ticket_type": "SINGLE"},
    ]
    return tickets


def write_tickets_json(tickets: List[Dict[str, str]], path: Union[str, Path]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"tickets": tickets}, fh, indent=2)
        fh.write("\n")
    return path


def qr_payload(ticket_id: str, payload_format: str = "plain") -> str:
    if payload_format == "plain":
        return ticket_id
    if payload_format == "prefixed":
        return f"TICKET:{ticket_id}"
    if payload_format == "json":
        return json.dumps({"ticket_id": ticket_id})
    raise ValueError(f"Unknown payload format {payload_format!r}")


def write_qr_images(
    tickets: List[Dict[str, str]],
    out_dir: Union[str, Path],
    payload_format: str = "plain",
    box_size: int = 10,
) -> List[Path]:
    import qrcode

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ticket in tickets:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=4)
        qr.add_data(qr_payload(ticket["ticket_id"], payload_format))
        qr.make(fit=True)
        path = out_dir / f"{ticket['ticket_id']}.png"
        qr.make_image(fill_color="black", back_color="white").save(str(path))
        paths.append(path)
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate demo tickets and QR codes.")
    parser.add_argument("--output", default=str(DEFAULT_TICKETS_PATH), help="tickets.json path")
    parser.add_argument("--qr-dir", default=str(DEFAULT_QR_DIR), help="directory for QR PNGs")
    parser.add_argument("--no-qr", action="store_true", help="only write tickets.json")
    parser.add_argument("--valid-count", type=int, default=5, help="number of ACTIVE tickets")
    parser.add_argument("--payload-format", choices=("plain", "prefixed", "json"), default="plain")
    args = parser.parse_args(argv)

    tickets = build_demo_tickets(valid_count=args.valid_count)
    json_path = write_tickets_json(tickets, args.output)
    print(f"Wrote {len(tickets)} tickets to {json_path}")
    if not args.no_qr:
        images = write_qr_images(tickets, args.qr_dir, args.payload_format)
        print(f"Wrote {len(images)} QR codes to {Path(args.qr_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
