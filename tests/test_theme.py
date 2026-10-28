"""dashboard/components/theme.py: contrast rules and badge styling."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

from dashboard.components import icons, nav, styles, theme, tokens

ROOT = Path(__file__).resolve().parents[1]


def test_contrast_reference_values():
    assert theme.contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert theme.contrast_ratio(theme.PRIMARY, theme.CARD) == pytest.approx(3.83, abs=0.01)


@pytest.mark.parametrize("text", [theme.TEXT, theme.MUTED, theme.SUCCESS, theme.ERROR, theme.WARNING,
                                  theme.PRIMARY_TEXT, theme.PURPLE_TEXT])
@pytest.mark.parametrize("bg", [theme.CARD, theme.BG])
def test_text_colours_meet_4_5(text, bg):
    assert theme.contrast_ratio(text, bg) >= 4.5


@pytest.mark.parametrize("accent", [theme.PRIMARY, theme.SUCCESS, theme.ERROR, theme.WARNING, theme.PURPLE])
def test_accents_as_marks_and_badges(accent):
    assert theme.contrast_ratio(accent, theme.CARD) >= 3.0
    assert theme.contrast_ratio(theme.TEXT, theme.tint(accent)) >= 4.5


def test_badge_style_never_uses_low_contrast_text():
    for label in theme.LABEL_COLORS:
        style = theme.badge_style(label)
        colour = style.split(";")[0]
        assert colour == f"color: {theme.TEXT}"
        assert theme.PRIMARY not in colour and theme.PURPLE not in colour
    assert theme.badge_style("unknown") == ""


def test_css_has_responsive_rules_and_every_slug():
    css = theme.CSS
    for rule in ("max-width: 1100px", "max-width: 480px", "pointer: coarse", "overflow-x: hidden"):
        assert rule in css
    for slug in theme.KPI_SLUGS.values():
        assert f".st-key-kpi-{slug}" in css


def test_chip_escapes_text():
    html = theme.chip("<b>x</b>", "success", title='a"b')
    assert "<b>" not in html and 'role="status"' in html and "&quot;" in html


def test_streamlit_config_keys_are_known():
    import streamlit.config as config

    data = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    keys = [f"theme.{k}" for k in data["theme"] if k != "sidebar"]
    keys += [f"theme.sidebar.{k}" for k in data["theme"]["sidebar"]]
    assert [k for k in keys if k not in config._config_options_template] == []
    assert data["theme"]["backgroundColor"] == theme.BG


# ------------------------------------------------------------------ mockup redesign tokens and CSS
def test_tokens_reexported_by_theme():
    for name in tokens.__all__:
        assert getattr(theme, name) is getattr(tokens, name), name
    assert theme.OUTCOME_COLORS["VERIFICATION_REQUIRED"] == theme.PURPLE
    assert theme.LABEL_COLORS["Ticket verification required"] == theme.PURPLE
    assert theme.CSS == theme.BASE_CSS + styles.LAYOUT_CSS


@pytest.mark.parametrize("colour", [tokens.TEXT, tokens.MUTED, tokens.SUCCESS, tokens.ERROR_TEXT])
@pytest.mark.parametrize("accent", [tokens.SUCCESS, tokens.ERROR, tokens.PRIMARY, tokens.PURPLE, tokens.WARNING])
def test_kpi_text_contrast(accent, colour):
    assert tokens.contrast_ratio(colour, tokens.tint(accent, .10)) >= 4.5


def test_white_text_on_strong_accents():
    assert tokens.contrast_ratio("#FFFFFF", tokens.PRIMARY_STRONG) >= 4.5
    assert tokens.contrast_ratio("#FFFFFF", tokens.ERROR_STRONG) >= 4.5


@pytest.mark.parametrize("stop", tokens.BANNER_STOPS)
def test_banner_text_contrast(stop):
    assert tokens.contrast_ratio(tokens.TEXT, stop) >= 4.5
    assert tokens.contrast_ratio(tokens.tint("#FFFFFF", .9, stop), stop) >= 4.5


@pytest.mark.parametrize("action", nav.QUICK_ACTIONS, ids=[a[0] for a in nav.QUICK_ACTIONS])
def test_quick_action_tile_text_contrast(action):
    colour = action[5]
    for stop in (tokens.tint(colour, .55), tokens.tint(colour, .25)):
        assert tokens.contrast_ratio(tokens.TEXT, stop) >= 4.5


def test_alert_panel_text_contrast():
    assert tokens.contrast_ratio(tokens.TEXT, tokens.tint(tokens.ERROR, .14)) >= 4.5


def test_chrome_css_keeps_sidebar_expand_control():
    css = styles.LAYOUT_CSS
    assert "stExpandSidebarButton" in css
    assert not re.search(r'stToolbar"\][^{]*\{[^}]*display:\s*none', css)
    for body in re.findall(r'stHeader"\][^{]*\{([^}]*)\}', css):
        assert not re.search(r"height:\s*0", body)
    assert re.search(r'stHeader"\][^{]*\{[^}]*pointer-events:\s*none', css)
    assert re.search(r'stExpandSidebarButton"\][^{]*\{[^}]*pointer-events:\s*auto', css)


def test_sidebar_width_only_while_expanded():
    css = styles.LAYOUT_CSS
    rules = re.findall(r"([^{}]*)\{[^}]*min-width:\s*220px", css)
    assert rules and all('[aria-expanded="true"]' in selector for selector in rules)


def test_layout_css_generated_rules():
    css = styles.LAYOUT_CSS
    for slug, _title, subtitle, *_ in nav.QUICK_ACTIONS:
        assert f'.st-key-qa-{slug} a p::after {{ content: "{subtitle}";' in css
    for stop in tokens.BANNER_STOPS:
        assert stop in css
    for name in ("tpd-delta--good", "tpd-delta--bad", "tpd-delta--flat", ".tpd-sr", ".st-key-tpd-bell",
                 "stPopoverBody", ".st-key-dash_notify button", ".st-key-feed-track-link", ".st-key-dash-mid", ".st-key-dash-bottom",
                 tokens.ERROR_STRONG, tokens.PRIMARY_STRONG, ":has("):
        assert name in css, name
    assert "animation: none; transition: none" in css


def test_nav_items_order_and_titles():
    assert [item[0] for item in nav.NAV_ITEMS] == ["Dashboard", "Live Feed", "Passenger Tracking", "Ticket Checker",
                                                  "Reports", "Settings"]
    assert [item[1] for item in nav.NAV_ITEMS] == [nav.PAGE_DASHBOARD, nav.PAGE_LIVE, nav.PAGE_TRACKING,
                                                  nav.PAGE_TICKET, nav.PAGE_REPORTS, nav.PAGE_SETTINGS]
    assert len({item[3] for item in nav.NAV_ITEMS}) == 6
    assert [a[0] for a in nav.QUICK_ACTIONS] == ["scan", "live", "track", "report"]


def test_ticketless_terms():
    assert theme.TICKETLESS_TERMS == ("Ticketless / Invalid QR", "Ticketless Passenger Detected!",
                                      "No ticketless passengers", "for ticketless travel")
    assert theme.TICKETLESS_SERIES == "Ticketless"


LEAF_MODULES = ("tokens", "nav", "icons", "styles", "cards", "notification_cards")


@pytest.mark.parametrize("module", LEAF_MODULES)
def test_leaf_modules_do_not_import_shell(module):
    source = (ROOT / "dashboard" / "components" / f"{module}.py").read_text(encoding="utf-8")
    imported = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
    parts = {part for name in imported for part in name.split(".")}
    assert not parts & {"shell", "actions", "theme"}, imported


def test_icons_are_hidden_inline_svgs():
    names = ("BUS", "CALENDAR", "BELL", "CHECK", "X", "PEOPLE", "SCAN", "CLOCK", "WARNING", "LIST", "SHIELD",
             "CAMERA_OFF")
    for name in names:
        svg = getattr(icons, name)
        assert svg.startswith("<svg") and 'aria-hidden="true"' in svg and 'fill="currentColor"' in svg
        assert 'viewBox="0 0 24 24"' in svg


def test_icon_renders_as_sanitizer_safe_img():
    """st.html strips <svg>; icons.icon() must emit an <img> with a decodable SVG data URI in the given colour."""
    import base64
    out = icons.icon(icons.CHECK, "#22C55E", 28)
    assert out.startswith('<img class="tpd-icon" src="data:image/svg+xml;base64,') and "<svg" not in out
    assert 'width="28"' in out and 'height="28"' in out and 'alt=""' in out
    decoded = base64.b64decode(out.split("base64,")[1].split('"')[0]).decode()
    assert 'xmlns="http://www.w3.org/2000/svg"' in decoded and 'fill="#22C55E"' in decoded


def _imports(module: str) -> set:
    source = (ROOT / "dashboard" / "components" / f"{module}.py").read_text(encoding="utf-8")
    imported = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
    return {part for name in imported for part in name.split(".")}


def test_nav_stays_a_leaf_with_shared_keys():
    from dashboard.components import shell

    parts = _imports("nav") - {"__future__", "annotations", "html", "typing", "List", "Tuple", "streamlit"}
    assert parts <= {"dashboard", "components", "tokens", "icons"}, parts  # icons is itself a leaf
    assert shell.TOAST_KEY == nav.TOAST_KEY == "_tpd_toast" and nav.ROUTER_KEY == "_tpd_router"


def test_panel_styles_is_a_leaf_and_injected():
    assert _imports("panel_styles") - {"__future__", "annotations"} <= {
        "dashboard", "components", "tokens", "BORDER", "CARD", "CARD_ALT", "MUTED", "PURPLE", "TEXT",
        "WARNING", "tint"}
    from dashboard.components import panel_styles

    for rule in (".tpd-metric-grid", "repeat(4, minmax(0, 1fr))", "max-width: 1100px", "max-width: 480px",
                 "overflow-wrap: anywhere", ".tpd-eval-meta--dataset", "repeat(6, minmax(0, 1fr))",
                 ".tpd-synthetic-pill", ".st-key-metric-dataset-buttons"):
        assert rule in panel_styles.METRICS_CSS, rule
    assert not hasattr(panel_styles, "AUTH_CSS")
    assert "transition: none" in panel_styles.METRICS_CSS
    assert "signout" not in styles.LAYOUT_CSS


def test_injected_css_has_no_tag_like_text():
    """DOMPurify (st.html) drops a <style> whose text contains tag-like sequences such as "<img" or "</"."""
    import re
    from dashboard.components import panel_styles, theme
    assert not re.search(r"</?[A-Za-z!]", theme.CSS + panel_styles.METRICS_CSS)
