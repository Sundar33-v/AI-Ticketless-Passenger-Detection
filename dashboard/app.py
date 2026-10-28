"""Conductor dashboard (Streamlit) router: six pages behind one sidebar navigation.

    streamlit run dashboard/app.py      (or scripts\\run_dashboard.bat, bound to 127.0.0.1)

Reads the same SQLite database as the detection process (``TPD_DB_PATH`` or
``database.path``). The dashboard has no sign-in: anyone who can reach the
bound address has full access. Dashboard writers: the Ticket Checker (ticket
checks), the metrics calculator's explicit Save Evaluation (manual
evaluation runs) and explicit Mark as read / Notify Conductor (alert_reads).
The dashboard never opens a camera or loads a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Streamlit puts dashboard/ first on sys.path, where this file (app.py) would shadow the
# project's `app` package. Always force the project root to the front.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]  # drop a cached non-package `app` module

import streamlit as st  # noqa: E402

from dashboard.components import nav  # noqa: E402

st.set_page_config(page_title="AI Ticketless Passenger Detection – Conductor", layout="wide",
                   initial_sidebar_state="auto")
st.session_state[nav.ROUTER_KEY] = True
# Streamlit's default page list is hidden; shell.start_page renders the custom sidebar nav (nav.NAV_ITEMS).
page = st.navigation(nav.build_pages(), position="hidden")
page.run()
