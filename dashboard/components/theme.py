"""Dark conductor theme: colour maps, CSS and small escaped HTML helpers.

The colour tokens and contrast maths live in ``tokens`` (re-exported here, so
``theme.BG``, ``theme.tint`` ... keep working). PRIMARY and PURPLE are below
4.5:1 on cards, so they are used only for non-text marks (borders, dots,
icons, chart marks); coloured text uses PRIMARY_TEXT / PURPLE_TEXT. Every
status is text plus colour.
"""

from __future__ import annotations

import html
from typing import Dict

import streamlit as st

from dashboard.components import panel_styles, styles
from dashboard.components.tokens import *  # noqa: F401,F403  (explicit __all__, incl. _rgb/_luminance)
from dashboard.components.tokens import (BG, BORDER, CARD, ERROR, MUTED, PRIMARY, PURPLE, SIDEBAR, SUCCESS,
                                         TEXT, TONE_COLORS, WARNING, tint)

BRAND_TITLE = "AI Ticketless Passenger Detection"
# The only phrases besides BRAND_TITLE that may say "ticketless": each describes completed failed
# ticket checks (Invalid KPI caption, alert headline bound to failed-check groups, banner subtitle).
TICKETLESS_TERMS = ("Ticketless / Invalid QR", "Ticketless Passenger Detected!", "No ticketless passengers",
                    "for ticketless travel")
# Plotly trace name of the failed-check series in the 10-minute trend (trace names are not scanned by
# the rendered-text test helpers; the series counts only INVALID / WRONG_ROUTE / ALREADY_USED checks).
TICKETLESS_SERIES = "Ticketless"

OUTCOME_COLORS: Dict[str, str] = {
    "VALID": SUCCESS, "INVALID": ERROR, "WRONG_ROUTE": ERROR, "ALREADY_USED": ERROR, "EXPIRED": WARNING,
    "VERIFICATION_REQUIRED": PURPLE,
}
LABEL_COLORS: Dict[str, str] = {
    "Valid": SUCCESS, "Invalid": ERROR, "Wrong route": ERROR, "Already used": ERROR, "Expired": WARNING,
    "Ticket verification required": PURPLE,
}
KPI_SLUGS: Dict[str, str] = {
    "Valid Tickets": "valid", "Invalid Tickets": "invalid", "Total Passenger": "passenger",
    "Total Detection": "detection", "Expired Tickets": "expired", "Boarding Events": "boarding",
    "Tracking IDs": "tracking", "Accuracy Score": "accuracy", "Precision": "precision",
    "Recall (Sensitivity)": "recall", "F1-Score": "f1",
}
CATEGORY_COLORS: Dict[str, str] = {
    "valid": SUCCESS, "invalid": ERROR, "expired": WARNING, "passenger": PRIMARY, "boarding": PRIMARY,
    "detection": MUTED, "tracking": MUTED, "accuracy": PURPLE, "precision": PURPLE, "recall": PURPLE, "f1": PURPLE,
}


def badge_style(label: str) -> str:
    """Cell style for an outcome label: TEXT on an 18 % tint with an accent border (never colour-only)."""
    accent = LABEL_COLORS.get(str(label))
    if accent is None:
        return ""
    return (f"color: {TEXT}; background-color: {tint(accent)}; font-weight: 600; "
            f"border-left: 3px solid {accent}")


# ------------------------------------------------------------------ HTML helpers
_MD_SPECIAL = set("\\`*_[]<>#|~$")


def md_escape(text: object) -> str:
    """Escape Markdown control characters so ticket refs such as ``TKT_*1`` render literally."""
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(text))


def chip(text: str, tone: str = "muted", *, title: str = "") -> str:
    colour = TONE_COLORS.get(tone, MUTED)
    tip = f' title="{html.escape(title)}"' if title else ""
    label = html.escape(text)
    return (f'<span class="tpd-chip" role="status" aria-label="{label}"{tip}>'
            f'<span class="tpd-dot" aria-hidden="true" style="background:{colour}"></span>{label}</span>')


def _kpi_rules() -> str:
    return "\n".join(
        f'.st-key-kpi-{slug} [data-testid="stMetric"] {{ border-left: 4px solid {colour}; }}\n'
        f'.st-key-kpi-{slug} [data-testid="stMetric"] [data-testid="stIconMaterial"] {{ color: {colour}; }}'
        for slug, colour in CATEGORY_COLORS.items())


BASE_CSS = f"""
[data-testid="stApp"] {{ background: {BG}; color: {TEXT}; }}
html, body {{ overflow-x: hidden; }}
[data-testid="stSidebar"] {{ background: {SIDEBAR}; }}
[data-testid="stMainBlockContainer"] {{ padding-top: 1rem; max-width: 100%; }}
[data-testid="stCaptionContainer"] {{ color: {MUTED}; }}
[data-testid="stMetric"], [class*="st-key-card-"] {{
  background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.35); padding: 0.75rem 1rem;
}}
[data-testid="stMetricValue"] {{ font-size: 1.6rem; font-weight: 600; }}
[data-testid="stMetricLabel"] {{ font-size: 0.85rem; color: {MUTED}; }}
{_kpi_rules()}
.tpd-header {{ display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .75rem; }}
.tpd-brand {{ display: flex; align-items: center; gap: .5rem; margin-right: auto; }}

.tpd-title {{ font-weight: 700; font-size: 1.05rem; color: {TEXT}; line-height: 1.2; }}
.tpd-subtitle {{ font-size: .8rem; color: {MUTED}; }}
.tpd-chip {{ display: inline-flex; align-items: center; gap: .4rem; padding: .2rem .65rem; font-size: .8rem;
  border: 1px solid {BORDER}; border-radius: 999px; background: {CARD}; color: {TEXT}; }}
.tpd-dot {{ width: .55rem; height: .55rem; border-radius: 50%; display: inline-block; }}
[data-testid="stImage"] img {{ max-width: 100%; height: auto; }}
[data-testid="stMetric"], [class*="st-key-card-"], .tpd-chip {{ animation: none; transition: none; }}
@media (max-width: 1100px) {{
  .st-key-kpi-grid [data-testid="stHorizontalBlock"], .st-key-model-grid [data-testid="stHorizontalBlock"],
  .st-key-mini-kpis [data-testid="stHorizontalBlock"], .st-key-charts [data-testid="stHorizontalBlock"] {{
    flex-wrap: wrap; }}
  .st-key-kpi-grid [data-testid="stColumn"], .st-key-model-grid [data-testid="stColumn"],
  .st-key-mini-kpis [data-testid="stColumn"] {{
    flex: 1 1 calc(50% - 1rem) !important; min-width: calc(50% - 1rem) !important; }}
  .st-key-charts [data-testid="stColumn"] {{ flex: 1 1 100% !important; min-width: 100% !important; }}
}}
@media (max-width: 480px) {{
  .st-key-kpi-grid [data-testid="stColumn"], .st-key-model-grid [data-testid="stColumn"],
  .st-key-mini-kpis [data-testid="stColumn"] {{ flex: 1 1 100% !important; min-width: 100% !important; }}
}}
@media (pointer: coarse) {{
  button, [data-testid="stPageLink"] a, [data-baseweb="select"] {{ min-height: 44px; }}
}}
"""
CSS = BASE_CSS + styles.LAYOUT_CSS


def inject_css() -> None:
    """The single CSS injection point: base + layout + metrics panel CSS."""
    css = CSS + panel_styles.METRICS_CSS
    st.html("<style>" + css + "</style>")
