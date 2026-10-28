"""Ticket Check: the conductor's manual validation workflow (the only page that writes ticket checks).

A check is made against the ticket record in the database. QR images carry
only an opaque token; nothing else in the QR code is trusted. Selecting the
boarding links the result to that passenger's boarding event, which resolves
"Ticket verification required" or a failed verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Streamlit puts dashboard/ first on sys.path, where dashboard/app.py would shadow the
# project's `app` package. Always force the project root to the front.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]  # drop a cached non-package `app` module

import dataclasses  # noqa: E402
import sqlite3  # noqa: E402
from typing import Optional, Tuple  # noqa: E402

import streamlit as st  # noqa: E402

from app.alert_service import outcome_label  # noqa: E402
from app.analytics import boardings_needing_resolution, inherited_trip_id  # noqa: E402
from app.database import parse_iso, utc_now  # noqa: E402
from app.passenger_service import is_busy_error  # noqa: E402
from app.qr_service import QRScanner  # noqa: E402
from app.ticket_validator import Outcome, TicketValidator, build_journey  # noqa: E402
from dashboard.components import data, filters, shell  # noqa: E402

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
INPUT_TYPES = ("Scan QR image", "Enter token", "Enter ticket ID")
NO_STATION = "(not set – skip station checks)"
RECHECK_HELP = ("Without selecting a boarding, repeat checks of the same ticket on the same trip are treated "
                "as re-checks (VALID). Select the boarding to detect reuse.")

ctx = shell.start_page("Ticket Checker", filters=False)   # css, require_database, Database caption, header
cfg = ctx.cfg
tz = ctx.tz
st.caption("Check a ticket against the database. A person is never marked as failing a check "
           "until a ticket has actually been checked.")
ctx.sidebar.header("Boardings")
period = filters.select_period(cfg, utc_now(), key="check_period", target=ctx.sidebar)

with data.read_conn() as _conn:
    pending = boardings_needing_resolution(_conn, period, cfg.dashboard.table_limit)
labels = {None: "(no boarding – desk check)"}
for row in pending.itertuples(index=False):
    boarded = parse_iso(row.boarded_at).astimezone(tz).strftime("%H:%M")
    labels[int(row.boarding_id)] = (f"Track ID {int(row.track_id)} – {boarded} – "
                                    f"{outcome_label(row.verification_status)}")

stations = [NO_STATION, *build_journey(cfg, now=utc_now()).ordered_stations]
current = cfg.journey.current_station
station_index = stations.index(current) if current in stations else 0

with st.form("ticket_check"):
    input_type = st.radio("Ticket presented as", INPUT_TYPES, horizontal=True)
    upload = st.file_uploader("QR code image (PNG/JPG, up to 5 MB)", type=["png", "jpg", "jpeg"])
    typed = st.text_input("Token or ticket ID", max_chars=128)
    boarding_id = st.selectbox("Boarding event (optional)", list(labels), format_func=labels.get,
                               help=RECHECK_HELP)
    station = st.selectbox("Current station", stations, index=station_index)
    no_ticket = st.checkbox("Record that no ticket was shown")
    submitted = st.form_submit_button("Check ticket", type="primary")


def read_credential() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(credential, credential_type, error)``; credential None means nothing was entered."""
    if input_type == "Scan QR image":
        if upload is None:
            return None, None, None
        content = upload.getvalue()
        if len(content) > MAX_UPLOAD_BYTES:
            return None, None, "The image is larger than 5 MB."
        try:
            codes = QRScanner().decode_image(content)
        except ValueError:
            return None, None, "Could not read the image."
        if not codes:
            return None, None, "No QR code found in the image"
        if len(codes) > 1:
            return None, None, "Several QR codes found; upload one at a time"
        return codes[0].payload, "qr_token", None
    text = typed.strip()
    if not text:
        return None, None, None
    return text, ("ticket_id" if input_type == "Enter ticket ID" else "qr_token"), None


def check_ticket(credential: Optional[str], credential_type: str) -> None:
    now = utc_now()
    try:
        with data.write_conn() as conn:
            trip_id = inherited_trip_id(conn, boarding_id=boarding_id, now=now,
                                        stale_sec=cfg.runtime.heartbeat_stale_sec)
            journey = build_journey(cfg, now=now, station=None if station == NO_STATION else station,
                                    trip_id=trip_id)
            if station == NO_STATION:
                journey = dataclasses.replace(journey, current_station=None)
            result = TicketValidator(tz=tz).validate(
                conn, credential, credential_type=credential_type, journey=journey, channel="dashboard",
                boarding_id=boarding_id, now=now)
    except sqlite3.OperationalError as exc:
        st.error("Database busy, try again" if is_busy_error(exc) else f"Database error: {exc}")
        return
    except ValueError as exc:
        st.error(str(exc))
        return
    message = f"{outcome_label(result.outcome)} – {result.reason}"
    if result.ticket_id:
        message += f" (ticket {result.ticket_id})"
    if result.outcome is Outcome.VALID:
        st.success(message)
    elif result.outcome is Outcome.VERIFICATION_REQUIRED:
        st.warning(message)
    else:
        st.error(message)


if submitted:
    credential, credential_type, error = read_credential()
    if error:
        st.error(error)
    elif credential is not None:
        check_ticket(credential, credential_type)
    elif no_ticket:
        check_ticket(None, "none")
    else:
        st.info("Nothing was checked. Upload a QR image or type a token / ticket ID, or tick "
                "'Record that no ticket was shown'.")
