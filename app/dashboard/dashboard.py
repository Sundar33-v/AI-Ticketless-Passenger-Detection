"""Conductor dashboard for AI Ticketless Passenger Detection.

    streamlit run app/dashboard/dashboard.py
    streamlit run app/dashboard/dashboard.py -- --db data/passengers.db --tickets data/tickets.json

Reads passenger events from the SQLite database written by ``python -m app.main``
and lets the conductor verify tickets by hand. Detection results are estimates:
PENDING / NEEDS_CHECK only mean "check this passenger", never "ticketless".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # `streamlit run` only adds the script's own folder
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.database import (  # noqa: E402
    DEFAULT_DB_PATH,
    EventNotFoundError,
    PassengerEvent,
    PassengerRepository,
    PassengerStatus,
    TicketStatus,
    init_db,
)

DEFAULT_TICKETS_PATH = PROJECT_ROOT / "data" / "tickets.json"
STATUS_ICONS = {
    PassengerStatus.VERIFIED.value: "🟢 VERIFIED",
    PassengerStatus.PENDING.value: "🟡 PENDING",
    PassengerStatus.NEEDS_CHECK.value: "🔴 NEEDS_CHECK",
}
TABLE_COLUMNS = [
    "Event ID", "Track ID", "Entry time", "Exit time",
    "Ticket ID", "Ticket status", "Passenger status",
]


# ------------------------------------------------------------------ helpers
def parse_cli_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Args after ``--`` in ``streamlit run dashboard.py -- --db ...``."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", default=None)
    parser.add_argument("--tickets", default=None)
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return args


def to_local(value: Optional[datetime]) -> Optional[datetime]:
    """DB stores naive UTC; show the conductor local time."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def events_to_frame(events: List[PassengerEvent]) -> pd.DataFrame:
    rows = [
        {
            "Event ID": e.event_id,
            "Track ID": e.track_id,
            "Entry time": to_local(e.entry_time),
            "Exit time": to_local(e.exit_time),
            "Ticket ID": e.ticket_id or "—",
            "Ticket status": e.ticket_status.value if e.ticket_status else "NOT SCANNED",
            "Passenger status": STATUS_ICONS.get(e.passenger_status.value, e.passenger_status.value),
        }
        for e in events
    ]
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


def summarize(counts: Dict[str, int]) -> Dict[str, int]:
    return {
        "total": sum(counts.values()),
        "verified": counts.get(PassengerStatus.VERIFIED.value, 0),
        "pending": counts.get(PassengerStatus.PENDING.value, 0),
        "needs_check": counts.get(PassengerStatus.NEEDS_CHECK.value, 0),
    }


def event_label(e: PassengerEvent) -> str:
    entry = to_local(e.entry_time)
    when = entry.strftime("%H:%M:%S") if entry else "?"
    return f"#{e.event_id} · Track {e.track_id} · {when} · {e.passenger_status.value}"


@st.cache_resource(show_spinner=False)
def get_repository(db_path: str) -> PassengerRepository:
    return PassengerRepository(init_db(db_path))


@st.cache_resource(show_spinner=False)
def get_validator(tickets_path: str, _mtime: float) -> Any:
    """Cached per file modification time so edits to tickets.json are picked up."""
    from app.ticket import TicketValidator

    return TicketValidator(tickets_path)


def load_validator(tickets_path: str):
    path = Path(tickets_path)
    if not path.is_file():
        return None, (f"Ticket list not found at {path}. Generate demo tickets with "
                      "`python -m app.ticket.generate_test_tickets`.")
    try:
        return get_validator(str(path), path.stat().st_mtime), None
    except Exception as exc:  # malformed tickets.json etc.
        return None, f"Could not load tickets: {exc}"


# ----------------------------------------------------------------------- UI
def render_live(repo: PassengerRepository, status_filter: Optional[PassengerStatus], limit: int) -> None:
    try:
        counts = summarize(repo.status_counts())
        events = repo.list_events(passenger_status=status_filter, limit=limit)
    except Exception as exc:
        st.error(f"Could not read the database: {exc}")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Passenger events", counts["total"])
    c2.metric("🟢 Verified", counts["verified"])
    c3.metric("🟡 Pending", counts["pending"])
    c4.metric("🔴 Needs check", counts["needs_check"])

    st.subheader("Passenger events")
    if not events:
        st.info("No passenger events yet. Start detection with `scripts\\run_detection.bat` "
                "(or `python -m app.main`).")
    else:
        st.dataframe(
            events_to_frame(events),
            hide_index=True,
            width="stretch",
            column_config={
                "Entry time": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
                "Exit time": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
            },
        )
    st.caption(f"Last refreshed {datetime.now().strftime('%H:%M:%S')}")


def render_actions(repo: PassengerRepository, validator: Any, validator_error: Optional[str]) -> None:
    st.subheader("Conductor actions")
    try:
        open_events = [
            e for e in repo.list_events(limit=500)
            if e.passenger_status is not PassengerStatus.VERIFIED or e.exit_time is None
        ]
    except Exception as exc:
        st.error(f"Could not read the database: {exc}")
        return
    if not open_events:
        st.write("No events to act on.")
        return

    by_id = {e.event_id: e for e in open_events}
    event_id = st.selectbox(
        "Passenger event", list(by_id), format_func=lambda i: event_label(by_id[i]), key="action_event"
    )

    left, right = st.columns(2)
    with left:
        with st.form("verify_ticket", clear_on_submit=True):
            ticket_raw = st.text_input("Ticket ID (from QR or paper ticket)", placeholder="TKT-0001")
            submitted = st.form_submit_button("Verify ticket", disabled=validator is None)
        if validator is None and validator_error:
            st.warning(validator_error)
        if submitted and validator is not None:
            try:
                event, result = repo.validate_and_assign_ticket(event_id, ticket_raw, validator)
            except EventNotFoundError as exc:
                st.error(str(exc))
            else:
                text = (f"Event #{event.event_id}: ticket {result.status.value} — {result.reason}. "
                        f"Passenger status: {event.passenger_status.value}.")
                # Rerun so the metrics/table above reflect the change; show the result afterwards.
                st.session_state["flash"] = ("success" if result.status is TicketStatus.VALID else "error", text)
                st.rerun()
    flash = st.session_state.pop("flash", None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])

    with right:
        st.write("Other actions")
        if st.button("Mark NEEDS_CHECK", width="stretch"):
            repo.mark_needs_check(event_id)
            st.rerun()
        if st.button("Record exit", width="stretch", help="PENDING at exit becomes NEEDS_CHECK"):
            try:
                repo.record_exit(event_id)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()


def main() -> None:
    st.set_page_config(page_title="Conductor Dashboard", page_icon="🚌", layout="wide")
    cli = parse_cli_args()

    with st.sidebar:
        st.header("Settings")
        db_path = st.text_input("SQLite database", value=cli.db or str(DEFAULT_DB_PATH))
        tickets_path = st.text_input("Ticket list (JSON)", value=cli.tickets or str(DEFAULT_TICKETS_PATH))
        filter_choice = st.selectbox("Show passenger status", ["ALL"] + [s.value for s in PassengerStatus])
        limit = st.slider("Max rows", 10, 1000, 200, step=10)
        auto = st.toggle("Auto-refresh", value=True)
        interval = st.slider("Refresh every (s)", 1, 30, 2, disabled=not auto)

    st.title("🚌 Conductor Dashboard")
    st.caption(
        "AI-assisted passenger counting (YOLO person detection + ByteTrack + entry line). "
        "Counts are estimates and can miss or double-count people in crowds or poor light. "
        "PENDING / NEEDS_CHECK means *please check this passenger's ticket* — it is not proof "
        "that anyone is ticketless. No facial recognition is used."
    )

    try:
        repo = get_repository(db_path)
    except Exception as exc:
        st.error(f"Could not open database {db_path}: {exc}")
        st.stop()
    validator, validator_error = load_validator(tickets_path)
    status_filter = None if filter_choice == "ALL" else PassengerStatus(filter_choice)

    live = st.fragment(run_every=interval if auto else None)(render_live)
    live(repo, status_filter, limit)
    st.divider()
    render_actions(repo, validator, validator_error)


if __name__ == "__main__":
    main()
