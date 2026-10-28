"""CSS for the AI metrics calculator panel (``METRICS_CSS``).

Leaf module: imports only ``tokens``. ``theme.inject_css`` (the single CSS injection point)
appends ``METRICS_CSS`` on every page. Grids use ``minmax(0, 1fr)`` and long text wraps, so
nothing forces page-level horizontal scrolling. No animations or transitions.
"""

from __future__ import annotations

from dashboard.components.tokens import BORDER, CARD, CARD_ALT, MUTED, PURPLE, TEXT, WARNING, tint

METRICS_CSS = f"""
/* ---- AI Model Performance & Metrics Calculator */
.tpd-section-title {{ font-size: 1.1rem; margin: 0 0 .35rem; padding: 0; }}
.tpd-eval-source {{ font-size: .8rem; color: {MUTED}; overflow-wrap: anywhere; }}
.tpd-metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .85rem; }}
.tpd-metric {{ min-width: 0; padding: .85rem 1rem; border-radius: 12px; border: 1px solid {BORDER};
  border-top: 3px solid {PURPLE}; background: {tint(PURPLE, .10)}; }}
.tpd-metric-label {{ font-size: .85rem; color: {MUTED}; }}
.tpd-metric-value {{ font-size: 1.6rem; font-weight: 700; color: {TEXT}; line-height: 1.2; }}
.tpd-eval-meta {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: .6rem;
  margin-top: .6rem; }}
.tpd-eval-meta > div {{ min-width: 0; padding: .45rem .65rem; border-radius: 10px; background: {CARD_ALT};
  border: 1px solid {BORDER}; }}
.tpd-eval-meta dt {{ font-size: .72rem; color: {MUTED}; }}
.tpd-eval-meta dd {{ margin: 0; font-size: .85rem; color: {TEXT}; overflow-wrap: anywhere; }}
.tpd-eval-empty {{ margin-top: .6rem; padding: .6rem .8rem; border-radius: 10px; border: 1px dashed {BORDER};
  background: {CARD}; color: {TEXT}; font-size: .85rem; }}
.tpd-eval-notes {{ font-size: .8rem; color: {MUTED}; }}
.st-key-metric-buttons [data-testid="stHorizontalBlock"], .st-key-metric-buttons [data-testid="stVerticalBlock"] {{
  flex-wrap: wrap; }}
.st-key-metric-buttons {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
.tpd-eval-meta--dataset {{ grid-template-columns: repeat(6, minmax(0, 1fr)); }}
.tpd-synthetic-pill {{ display: inline-block; padding: 0 .45rem; border-radius: 999px; border: 1px solid {WARNING};
  background: {tint(WARNING, .12)}; color: {TEXT}; font-size: .75rem; }}
.st-key-metric-dataset-buttons [data-testid="stHorizontalBlock"],
.st-key-metric-dataset-buttons [data-testid="stVerticalBlock"] {{ flex-wrap: wrap; }}
.st-key-metric-dataset-buttons {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
.st-key-card-metrics .tpd-table td {{ white-space: nowrap; }}
.st-key-card-metrics .tpd-table td.tpd-wrap {{ white-space: normal; overflow-wrap: anywhere; min-width: 140px; }}
.tpd-metric, .tpd-eval-meta > div {{ animation: none; transition: none; }}
@media (max-width: 1100px) {{
  .tpd-metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .tpd-eval-meta {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .st-key-metric-inputs [data-testid="stHorizontalBlock"], .st-key-metric-details [data-testid="stHorizontalBlock"] {{
    flex-wrap: wrap; }}
  .st-key-metric-inputs [data-testid="stColumn"], .st-key-metric-details [data-testid="stColumn"] {{
    flex: 1 1 calc(50% - 1rem) !important; min-width: calc(50% - 1rem) !important; }}
}}
@media (max-width: 480px) {{
  .tpd-metric-grid, .tpd-eval-meta {{ grid-template-columns: minmax(0, 1fr); }}
  .st-key-metric-inputs [data-testid="stColumn"], .st-key-metric-details [data-testid="stColumn"] {{
    flex: 1 1 100% !important; min-width: 100% !important; }}
}}
"""
