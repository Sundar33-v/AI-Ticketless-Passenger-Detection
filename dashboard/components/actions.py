"""Quick Actions card: real page links (no simulated actions) and the AI Powered badge."""

from __future__ import annotations

import html

import streamlit as st

from dashboard.components import cards, icons, nav, shell
from dashboard.components.tokens import SUCCESS

DIRECT_CAPTION = "Quick actions: open via the sidebar (start the dashboard with scripts\\run_dashboard.bat)"
AI_TIP = "Model performance: see AI Model Performance & Metrics Calculator below"
AI_LINE = "Real-time alerts • Ticket verification • Safe travel"


_STATIC_ICONS = {"scan": icons.QR_SCAN, "live": icons.CAMERA, "track": icons.PIN, "report": icons.FILE_TEXT}


def _static_tiles() -> str:
    tiles = [f'<div class="tpd-qa tpd-qa--{html.escape(slug)}">'
             f'{icons.icon(_STATIC_ICONS[slug], cards.WHITE, 24)}<div><div>{html.escape(title)}</div>'
             f'<span class="tpd-qa-sub">{html.escape(subtitle)}</span></div></div>'
             for slug, title, subtitle, _page, _icon, _colour in nav.QUICK_ACTIONS]
    return '<div class="tpd-qa-list">' + "".join(tiles) + "</div>"


def ai_badge_html() -> str:
    return (f'<div class="tpd-ai-badge"><span class="tpd-ai-pill">{icons.icon(icons.SHIELD, SUCCESS, 18)}'
            ' AI Powered</span>'
            f'<span class="tpd-ai-line" title="{html.escape(AI_TIP)}">{html.escape(AI_LINE)}'
            f'<span class="tpd-sr"> ({html.escape(AI_TIP)})</span></span></div>')


def render_quick_actions_card() -> None:
    """Four tiles from ``nav.QUICK_ACTIONS`` (page links under the router, static tiles on direct runs)."""
    with st.container(border=True, key="card-actions"):
        st.html(cards.title_html("Quick Actions", icons.BOLT))
        if shell.under_router():
            for slug, title, subtitle, page, icon, _colour in nav.QUICK_ACTIONS:
                with st.container(key=f"qa-{slug}"):
                    st.page_link(page, label=title, icon=icon, help=subtitle, width="stretch")
        else:
            st.html(_static_tiles())
            st.caption(DIRECT_CAPTION)
        st.html(ai_badge_html())
