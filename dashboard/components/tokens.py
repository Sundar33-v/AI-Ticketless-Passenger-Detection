"""Colour tokens and contrast maths (standard library only; re-exported by ``theme``).

PRIMARY and PURPLE are below 4.5:1 on cards, so they are used only for
non-text marks (borders, dots, icons, chart marks); coloured text uses
PRIMARY_TEXT / PURPLE_TEXT. White text is never drawn on PRIMARY or ERROR:
those surfaces use PRIMARY_STRONG / ERROR_STRONG. Red text on tinted
surfaces uses ERROR_TEXT.
"""

from __future__ import annotations

from typing import Dict, Tuple

__all__ = [
    "BG", "SIDEBAR", "CARD", "CARD_ALT", "BORDER", "PRIMARY", "PRIMARY_STRONG",
    "SUCCESS", "ERROR", "ERROR_STRONG", "ERROR_TEXT", "BANNER_STOPS", "WARNING",
    "PURPLE", "SLATE", "TEXT", "MUTED", "PRIMARY_TEXT", "PURPLE_TEXT", "GLOW",
    "TONE_COLORS", "tint", "contrast_ratio", "_rgb", "_luminance",
]

BG, SIDEBAR, CARD, CARD_ALT, BORDER = "#0B1220", "#0D1526", "#111B2E", "#0F1828", "#1E2A44"
PRIMARY, PRIMARY_STRONG = "#2F6BFF", "#2563EB"
SUCCESS, ERROR, ERROR_STRONG, ERROR_TEXT = "#22C55E", "#EF4444", "#DC2626", "#F87171"
BANNER_STOPS: Tuple[str, str, str] = ("#1D4ED8", "#2456E0", "#1E3A8A")  # 0 %, 45 %, 100 %
WARNING, PURPLE, SLATE = "#F59E0B", "#8B5CF6", "#334155"
TEXT, MUTED = "#F5F8FF", "#94A3B8"
PRIMARY_TEXT, PURPLE_TEXT = "#6EA8FF", "#B48CFF"
GLOW = "0 0 0 1px rgba(47,107,255,.06), 0 8px 24px rgba(0,0,0,.35)"

TONE_COLORS: Dict[str, str] = {"success": SUCCESS, "warning": WARNING, "error": ERROR, "muted": MUTED,
                               "info": PRIMARY}


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def tint(accent: str, alpha: float = 0.18, base: str = CARD) -> str:
    """``accent`` mixed at ``alpha`` over ``base`` as an opaque hex colour."""
    a, b = _rgb(accent), _rgb(base)
    return "#" + "".join(f"{round(alpha * x + (1 - alpha) * y):02X}" for x, y in zip(a, b))


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio."""
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
