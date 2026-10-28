"""Layout CSS for the mockup shell: top bar, sidebar nav, banner, KPI grid, cards, table, tiles.

Leaf module: imports only ``tokens`` and ``nav`` (``theme`` appends ``LAYOUT_CSS`` to its base CSS).
Every value comes from the colour tokens; the quick-action subtitle rules are
generated from ``nav.QUICK_ACTIONS`` so the tile text and the CSS cannot drift.
No animations or transitions (reduced-motion stance).
"""

from __future__ import annotations

from dashboard.components import nav
from dashboard.components.tokens import (BANNER_STOPS, BORDER, CARD, CARD_ALT, ERROR, ERROR_STRONG, ERROR_TEXT,
                                         GLOW, MUTED, PRIMARY, PRIMARY_STRONG, PRIMARY_TEXT, PURPLE, SIDEBAR,
                                         SUCCESS, TEXT, WARNING, tint)

WHITE = "#FFFFFF"
PILL_ACCENTS = {"valid": SUCCESS, "invalid": ERROR, "expired": WARNING, "unverified": PURPLE}


def _css_string(text: str) -> str:
    """A CSS string literal for a module constant (quotes and backslashes escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quick_action_rules() -> str:
    rules = []
    for slug, _title, subtitle, _page, _icon, colour in nav.QUICK_ACTIONS:
        surface = (f"background: linear-gradient(135deg, {tint(colour, .55)}, {tint(colour, .25)}); "
                   f"border: 1px solid {tint(colour, .7)};")
        rules.append(f".st-key-qa-{slug} a, .tpd-qa--{slug} {{ {surface} }}")
        rules.append(f".st-key-qa-{slug} a p::after {{ content: {_css_string(subtitle)}; display: block; "
                     "font-size: .75rem; font-weight: 400; opacity: .85; }")
    return "\n".join(rules)


def _pill_rules() -> str:
    return "\n".join(f".tpd-pill--{name} {{ background: {tint(colour, .22)}; border-color: {colour}; }}"
                     for name, colour in PILL_ACCENTS.items())


_BANNER = ", ".join(f"{stop} {pos}" for stop, pos in zip(BANNER_STOPS, ("0%", "45%", "100%")))

LAYOUT_CSS = f"""
/* ---- chrome hiding and spacing (stToolbar itself stays: it holds the expand-sidebar button) */
#MainMenu, [data-testid="stMainMenu"], [data-testid="stToolbarActions"], [data-testid="stAppDeployButton"],
[data-testid="stStatusWidget"], [data-testid="stSidebarNav"], footer {{ display: none !important; }}
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stHeader"], [data-testid="stToolbar"] {{ pointer-events: none !important; }}
[data-testid="stExpandSidebarButton"] {{ color: {TEXT}; pointer-events: auto !important; }}
[data-testid="stAppViewContainer"]:has([data-testid="stExpandSidebarButton"]) .st-key-tpd-topbar {{
  margin-left: 3rem; width: calc(100% - 3rem) !important; }}
[data-testid="stMainBlockContainer"] {{ padding: .75rem 1.25rem 1.5rem; max-width: 100%; }}
[data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] {{ gap: .85rem; }}
[data-testid="stSidebarContent"] {{ padding-top: .75rem; }}
.tpd-sr {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}

/* ---- cards */
[class*="st-key-card-"], .tpd-card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
  box-shadow: {GLOW}; padding: .9rem 1rem; }}
.tpd-card-title {{ display: flex; align-items: center; gap: .5rem; font-size: 1rem; font-weight: 600;
  color: {TEXT}; margin: 0 0 .25rem; }}
.tpd-card-title .tpd-icon {{ width: 20px; height: 20px; }}

/* ---- icons: icons.icon() img elements (st.html strips inline svg markup) */
img.tpd-icon {{ display: block; flex: 0 0 auto; max-width: none; object-fit: contain; pointer-events: none;
  user-select: none; }}

/* ---- top bar */
.st-key-tpd-topbar {{ background: {SIDEBAR}; border: 1px solid {BORDER}; border-radius: 12px; padding: .6rem 1rem; }}
.tpd-brand-tile {{ display: inline-flex; align-items: center; justify-content: center; width: 52px; height: 52px;
  flex: 0 0 52px; border-radius: 12px; background: {tint(PRIMARY, .18)}; border: 1px solid {tint(PRIMARY, .45)}; }}
.st-key-tpd-topbar .tpd-title {{ font-size: 1.35rem; font-weight: 700; color: {TEXT}; }}
.st-key-tpd-topbar .tpd-subtitle {{ font-size: .82rem; color: {MUTED}; }}
.tpd-topbar-right {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: .85rem; }}
.tpd-datetime {{ display: inline-flex; align-items: center; gap: .45rem; font-size: .85rem; color: {TEXT}; }}

.tpd-time {{ color: {MUTED}; }}
.tpd-divider {{ width: 1px; height: 28px; background: {BORDER}; }}
.tpd-conductor {{ display: inline-flex; align-items: center; gap: .55rem; }}
.tpd-avatar {{ display: inline-flex; align-items: center; justify-content: center; width: 38px; height: 38px;
  flex: 0 0 38px; border-radius: 50%; background: {tint(PRIMARY, .35)}; overflow: hidden; }}
.tpd-conductor-name {{ font-size: .85rem; font-weight: 600; color: {TEXT}; line-height: 1.2; }}
.tpd-conductor-status {{ display: flex; align-items: center; gap: .35rem; font-size: .75rem; color: {MUTED}; }}
.tpd-status-dot {{ width: .5rem; height: .5rem; border-radius: 50%; display: inline-block; }}
.tpd-status-dot--online {{ background: {SUCCESS}; }}
.tpd-status-dot--offline {{ background: {ERROR}; }}

/* ---- bell trigger: fixed column, stable key; the label is sr-only, the badge is its own element */
.st-key-tpd-topbar [data-testid="stColumn"]:has(.st-key-tpd-bell) {{ flex: 0 0 52px !important;
  width: 52px !important; min-width: 52px !important; }}
.st-key-tpd-bell {{ position: relative; align-items: flex-end; }}
.st-key-tpd-bell [data-testid="stPopoverButton"] {{ position: relative; width: 40px; min-width: 40px; height: 40px;
  padding: 0; border-radius: 50%; background: {CARD}; border: 1px solid {BORDER}; color: {TEXT};
  justify-content: center; }}
.st-key-tpd-bell [data-testid="stPopoverButton"]:focus-visible {{ outline: 2px solid {PRIMARY_TEXT};
  outline-offset: 2px; }}
.st-key-tpd-bell [data-testid="stPopoverButton"] p {{ position: absolute; width: 1px; height: 1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; margin: -1px; padding: 0; border: 0; }}
.st-key-tpd-bell [data-testid="stPopoverButton"] > div > [aria-hidden="true"]:last-child {{ display: none; }}
.st-key-tpd-bell [data-testid="stElementContainer"]:has(.tpd-bell-badge-wrap) {{ position: absolute; top: -6px;
  right: -6px; width: auto !important; z-index: 2; pointer-events: none; }}
.tpd-bell-badge {{ display: inline-block; min-width: 18px; height: 18px; padding: 0 4px; border-radius: 999px;
  background: {ERROR_STRONG}; color: {WHITE}; font-size: 11px; font-weight: 700; line-height: 18px;
  text-align: center; }}
.tpd-bell-badge[hidden] {{ display: none; }}
.tpd-bell-badge--error, .tpd-bell-badge--loading {{ background: {BORDER}; color: {TEXT}; }}

/* ---- notification panel (popover portal): scoped by the bell-panel key placed inside it */
[data-testid="stPopoverBody"]:has(.st-key-bell-panel) {{ width: min(420px, calc(100vw - 16px)); padding: 0;
  background: {CARD}; color: {TEXT}; border: 1px solid {tint(PRIMARY, .45)}; border-radius: 12px;
  box-shadow: 0 18px 48px rgba(0,0,0,.6), 0 0 0 1px rgba(0,0,0,.35); overflow-y: auto;
  overscroll-behavior: contain; }}
.st-key-bell-panel {{ padding: 0 .9rem .9rem; gap: .5rem; }}
.st-key-bell-head {{ position: sticky; top: 0; z-index: 1; background: {CARD}; padding: .75rem 0 .5rem;
  border-bottom: 1px solid {BORDER}; justify-content: space-between; align-items: center; }}
/* Streamlit 1.64 wraps the keyed horizontal container in a same-height stLayoutWrapper, which would
   confine the sticky head; the wrapper itself is made sticky so the head stays on top while scrolling. */
[data-testid="stLayoutWrapper"]:has(> .st-key-bell-head) {{ position: sticky; top: 0; z-index: 1;
  background: {CARD}; }}
.tpd-bell-head h2 {{ margin: 0; padding: 0; font-size: 1rem; font-weight: 700; color: {TEXT}; }}
.tpd-bell-head p {{ margin: 0; font-size: .78rem; color: {MUTED}; }}
[class*="st-key-bell-item-"] {{ background: {CARD_ALT}; border: 1px solid {BORDER}; border-left: 3px solid {PRIMARY};
  border-radius: 10px; padding: .6rem .75rem; gap: .35rem; }}
[class*="st-key-bell-item-"]:has(.tpd-note--error) {{ border-left-color: {ERROR}; }}
[class*="st-key-bell-item-"]:has(.tpd-note--warning) {{ border-left-color: {WARNING}; }}
.tpd-note-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }}
.tpd-note-title {{ font-size: .9rem; font-weight: 700; color: {TEXT}; }}
.tpd-note--read .tpd-note-title {{ font-weight: 500; }}
.tpd-note-dot {{ width: .5rem; height: .5rem; border-radius: 50%; background: {PRIMARY_TEXT}; }}
.tpd-note--read .tpd-note-dot, .tpd-note--info .tpd-note-dot {{ background: transparent; border: 1px solid {MUTED}; }}
.tpd-note-pill {{ font-size: .7rem; padding: 0 .45rem; border-radius: 999px; border: 1px solid {BORDER};
  color: {MUTED}; }}
.tpd-note--unread .tpd-note-pill--state {{ color: {PRIMARY_TEXT}; border-color: {tint(PRIMARY, .6)}; }}
.tpd-note-msg {{ margin: .25rem 0; font-size: .82rem; line-height: 1.45; color: {TEXT}; overflow-wrap: anywhere; }}
.tpd-note-meta {{ display: flex; flex-wrap: wrap; gap: .25rem .75rem; font-size: .75rem; color: {MUTED}; }}
.st-key-bell-panel button p {{ color: {PRIMARY_TEXT}; font-size: .8rem; }}
.tpd-bell-state {{ padding: .75rem; border-radius: 10px; border: 1px dashed {BORDER}; background: {CARD_ALT};
  color: {TEXT}; font-size: .85rem; }}
.tpd-bell-state p {{ margin: 0; }}
.tpd-bell-state--error {{ border-color: {tint(ERROR, .6)}; }}
.tpd-bell-state--warning {{ border-color: {tint(WARNING, .6)}; }}
.tpd-note, [class*="st-key-bell-item-"] {{ animation: none; transition: none; }}

/* ---- sidebar and custom nav (width only while expanded: collapse and resizing stay Streamlit's) */
[data-testid="stSidebar"][aria-expanded="true"] {{ min-width: 220px; max-width: 220px; }}
[class*="st-key-nav-"] a, .tpd-nav-item {{ display: flex; align-items: center; gap: .6rem; min-height: 44px;
  padding: 0 .75rem; border-radius: 10px; color: {MUTED}; text-decoration: none; }}
[class*="st-key-nav-"] a p {{ color: inherit; }}
[class*="st-key-nav-"] a:hover, .tpd-nav-item:hover {{ background: {tint(PRIMARY, .12)}; color: {TEXT}; }}
.st-key-nav-active a, .tpd-nav-item.tpd-nav-active {{ background: {PRIMARY_STRONG}; color: {WHITE};
  box-shadow: 0 6px 18px rgba(47,107,255,.35); }}
.st-key-nav-active a p, .st-key-nav-active a [data-testid="stIconMaterial"], .tpd-nav-active {{ color: {WHITE}; }}
[class*="st-key-nav-"] a [data-testid="stIconMaterial"] {{ font-size: 22px; width: 22px; color: inherit;
  display: inline-flex; align-items: center; justify-content: center; flex: 0 0 22px; }}
.st-key-nav-active a:hover {{ background: {PRIMARY_STRONG}; color: {WHITE}; }}
.tpd-nav {{ display: flex; flex-direction: column; gap: .25rem; margin-bottom: .75rem; }}
.tpd-sidebar-footer {{ margin-top: 2rem; text-align: center; }}
.tpd-footer-bus {{ display: flex; justify-content: center; margin-bottom: .35rem; }}
.tpd-footer-bus .tpd-icon {{ opacity: .45; filter: drop-shadow(0 6px 14px rgba(47,107,255,.35)); }}
.tpd-tagline {{ display: block; font-size: .85rem; font-style: italic; color: {PRIMARY_TEXT}; }}

/* ---- welcome banner */
.tpd-banner {{ display: grid; grid-template-columns: auto 1fr auto auto; align-items: center; gap: 1.25rem;
  background: linear-gradient(110deg, {_BANNER}); border-radius: 14px; padding: 1.1rem 1.4rem; color: {WHITE}; }}
.tpd-banner-tile {{ display: inline-flex; align-items: center; justify-content: center; width: 68px; height: 68px;
  flex: 0 0 68px; border-radius: 16px; background: rgba(11,18,32,.45); border: 1px solid rgba(255,255,255,.18); }}
.tpd-banner h2 {{ margin: 0; padding: 0; font-size: 1.5rem; font-weight: 700; color: {WHITE}; line-height: 1.25; }}
.tpd-banner p {{ margin: .2rem 0 0; color: rgba(255,255,255,.9); font-size: .9rem; }}
.tpd-banner-pill {{ display: inline-flex; align-items: center; gap: .45rem; padding: .35rem .85rem;
  border-radius: 999px; background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.2);
  color: {WHITE}; font-size: .85rem; font-weight: 600; white-space: nowrap; }}
.tpd-route-card {{ display: flex; align-items: center; gap: .65rem; padding: .55rem .85rem; border-radius: 10px;
  background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.15); color: {WHITE}; }}
.tpd-route-tile {{ display: inline-flex; align-items: center; justify-content: center; width: 42px; height: 42px;
  flex: 0 0 42px; border-radius: 10px; background: rgba(11,18,32,.5); }}
.tpd-route-label {{ font-size: .75rem; color: rgba(255,255,255,.9); }}
.tpd-route-text {{ font-weight: 600; }}

/* ---- KPI row (accent colours are set per card by cards.kpi_cards_html) */
.tpd-kpis {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: .85rem; }}
.tpd-kpi {{ display: flex; align-items: center; gap: .85rem; padding: .9rem 1rem; border-radius: 12px;
  border: 1px solid {BORDER}; background: {CARD}; min-width: 0; }}
.tpd-kpi-icon {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 52px;
  width: 52px; height: 52px; border-radius: 50%; margin: 5px; }}
.tpd-kpi-body {{ min-width: 0; }}
.tpd-kpi-label {{ font-size: .85rem; color: {MUTED}; }}
.tpd-kpi-value {{ font-size: 1.9rem; font-weight: 700; color: {TEXT}; line-height: 1.1; }}
.tpd-delta {{ font-size: .75rem; font-weight: 600; }}
.tpd-delta--good {{ color: {SUCCESS}; }}
.tpd-delta--bad {{ color: {ERROR_TEXT}; }}
.tpd-delta--flat {{ color: {MUTED}; }}
.tpd-kpi-caption {{ font-size: .75rem; color: {MUTED}; }}

/* ---- donut legend */
.tpd-legend {{ display: flex; flex-direction: column; gap: .55rem; }}
.tpd-legend-row {{ display: flex; align-items: center; gap: .5rem; font-size: .85rem; color: {TEXT}; }}
.tpd-legend-dot {{ width: 10px; height: 10px; flex: 0 0 10px; border-radius: 50%; display: inline-block; }}
.tpd-legend-value {{ margin-left: auto; color: {MUTED}; white-space: nowrap; }}

/* ---- live feed card */
.tpd-live-dot {{ width: .6rem; height: .6rem; border-radius: 50%; background: {ERROR}; display: inline-block; }}
.st-key-feed-frame img {{ border-radius: 10px; }}
.tpd-feed-placeholder {{ aspect-ratio: 16 / 10; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: .5rem; padding: 1rem; background: #060B16; border: 1px solid {BORDER};
  border-radius: 10px; color: {MUTED}; font-size: .85rem; text-align: center; }}

.tpd-alert {{ padding: .85rem; border-radius: 10px; background: {tint(ERROR, .14)};
  border: 1px solid {tint(ERROR, .5)}; color: {TEXT}; }}
.tpd-alert--ok {{ background: {tint(SUCCESS, .10)}; border-color: {tint(SUCCESS, .45)}; }}
.tpd-alert-head {{ display: flex; align-items: center; gap: .6rem; margin-bottom: .45rem; }}
.tpd-alert-icon {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 34px;
  width: 34px; height: 34px; border-radius: 50%; background: {ERROR}; color: {WHITE}; }}
.tpd-alert--ok .tpd-alert-icon {{ background: {SUCCESS}; }}

.tpd-alert-title {{ font-weight: 700; color: {TEXT}; line-height: 1.3; }}
.tpd-alert-row {{ font-size: .85rem; color: {TEXT}; line-height: 1.6; }}
.st-key-dash_notify button {{ background: {ERROR_STRONG}; border: 1px solid {ERROR_STRONG}; color: {WHITE}; }}
.st-key-dash_notify button p {{ color: {WHITE}; }}
.st-key-dash_notify button:disabled {{ background: {tint(ERROR, .25)}; border-color: {tint(ERROR, .25)};
  color: {MUTED}; }}
.st-key-dash_notify button:disabled p {{ color: {MUTED}; }}
.st-key-feed-track-link a {{ justify-content: center; border-radius: 10px; background: transparent;
  border: 1px solid {tint(PRIMARY, .7)}; color: {TEXT}; }}

/* ---- validation table */
.tpd-table-wrap {{ overflow-x: auto; overflow-y: auto; max-height: 360px; border: 1px solid {BORDER};
  border-radius: 10px; }}
.tpd-table {{ width: 100%; border-collapse: collapse; font-size: .85rem; color: {TEXT}; }}
.tpd-table thead th {{ position: sticky; top: 0; z-index: 1; background: {SIDEBAR}; color: {MUTED};
  font-size: .75rem; font-weight: 600; text-transform: uppercase; text-align: left; padding: .55rem .6rem;
  border-bottom: 1px solid {BORDER}; white-space: nowrap; }}
.tpd-table tbody tr:nth-child(odd) {{ background: {CARD}; }}
.tpd-table tbody tr:nth-child(even) {{ background: {CARD_ALT}; }}
.tpd-table tbody tr:hover {{ background: {tint(PRIMARY, .08)}; }}
.tpd-table td {{ padding: .5rem .6rem; border-bottom: 1px solid {BORDER}; vertical-align: middle; }}
.tpd-table .tpd-reason {{ max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.tpd-table .tpd-empty {{ text-align: center; color: {MUTED}; padding: 1.25rem; }}
.tpd-pill {{ display: inline-block; padding: .1rem .6rem; border-radius: 999px; border: 1px solid {BORDER};
  color: {TEXT}; font-size: .75rem; font-weight: 600; white-space: nowrap; }}
{_pill_rules()}

/* ---- quick actions */
[class*="st-key-qa-"] a, .tpd-qa {{ display: flex; align-items: center; gap: .6rem; min-height: 64px;
  padding: .55rem .85rem; border-radius: 12px; color: {TEXT}; text-decoration: none; }}
[class*="st-key-qa-"] a p {{ color: {TEXT}; font-weight: 600; }}
[class*="st-key-qa-"] a [data-testid="stIconMaterial"] {{ font-size: 26px; width: 40px; height: 40px;
  flex: 0 0 40px; display: inline-flex; align-items: center; justify-content: center; border-radius: 10px;
  background: rgba(11,18,32,.35); color: {TEXT}; }}
.st-key-dash_notify button [data-testid="stIconMaterial"], .st-key-feed-track-link a [data-testid="stIconMaterial"] {{
  font-size: 20px; color: inherit; }}
.tpd-qa-sub {{ display: block; font-size: .75rem; font-weight: 400; opacity: .85; }}
.tpd-qa-list {{ display: flex; flex-direction: column; gap: .5rem; }}
{_quick_action_rules()}
.tpd-ai-badge {{ display: flex; flex-direction: column; gap: .35rem; margin-top: .5rem; }}
.tpd-ai-pill {{ display: inline-flex; align-items: center; gap: .4rem; align-self: flex-start;
  padding: .2rem .7rem; border-radius: 999px; border: 1px solid {SUCCESS}; background: {tint(SUCCESS, .14)};
  color: {TEXT}; font-size: .8rem; font-weight: 600; }}

.tpd-ai-line {{ font-size: .75rem; color: {MUTED}; }}

.tpd-banner, .tpd-kpi, .tpd-alert, .tpd-pill, .tpd-nav-item, [class*="st-key-nav-"] a, [class*="st-key-qa-"] a,
.tpd-table tbody tr {{ animation: none; transition: none; }}

/* ---- responsive (layout target 1366-1920 px) */
@media (max-width: 1100px) {{
  .tpd-kpis {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .tpd-banner {{ grid-template-columns: auto 1fr; }}
  .st-key-dash-mid [data-testid="stHorizontalBlock"], .st-key-dash-bottom [data-testid="stHorizontalBlock"] {{
    flex-wrap: wrap; }}
  .st-key-dash-mid [data-testid="stColumn"], .st-key-dash-bottom [data-testid="stColumn"] {{
    flex: 1 1 100% !important; min-width: 100% !important; }}
}}
@media (max-width: 640px) {{
  .st-key-tpd-topbar [data-testid="stColumn"]:has(.st-key-tpd-bell) {{ flex: 1 1 100% !important;
    width: auto !important; }}
}}
@media (max-width: 480px) {{
  .tpd-kpis {{ grid-template-columns: 1fr; }}
  .tpd-banner {{ grid-template-columns: 1fr; }}
  .st-key-tpd-topbar [data-testid="stColumn"]:has(.st-key-tpd-bell) {{ flex: 1 1 100% !important;
    width: auto !important; min-width: 0 !important; }}
}}
"""
