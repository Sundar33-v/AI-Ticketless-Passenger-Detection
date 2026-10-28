"""SVG icons (24x24 Material Design paths) and the ``icon()`` renderer used by every HTML builder.

Why ``icon()`` exists: ``st.html`` sanitizes its markup with DOMPurify using the HTML-only
profile (``USE_PROFILES: {html: true}``), which removes every ``<svg>`` / ``<path>`` element.
Inline SVG therefore renders as an empty box. ``icon()`` wraps the same SVG in an
``<img src="data:image/svg+xml;base64,...">`` element, which the sanitizer keeps (``img`` is
an allowed data-URI tag). An ``<img>`` cannot inherit ``currentColor``, so the fill colour is
baked in per call.

The raw constants below stay ``currentColor`` SVG strings (theme-neutral source of truth).
Leaf module: imports nothing from the dashboard. Icons render offline (no CDN).
"""

from __future__ import annotations

import base64
import html
from functools import lru_cache

_NS = 'xmlns="http://www.w3.org/2000/svg" '


def _svg(path: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="{path}"/></svg>'


BUS = _svg("M4 16c0 .88.39 1.67 1 2.22V20a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1h8v1a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1"
           "v-1.78c.61-.55 1-1.34 1-2.22V6c0-3.5-3.58-4-8-4s-8 .5-8 4v10zm3.5 1a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 "
           "0 3zm9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM18 11H6V6h12v5z")
CALENDAR = _svg("M20 3h-1V1h-2v2H7V1H5v2H4c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z"
                "m0 18H4V8h16v13zM6 10h5v5H6z")
BELL = _svg("M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5"
            "s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z")
USER = _svg("M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 "
            "3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 "
            "3.08-1.29 1.94-3.5 3.22-6 3.22z")
CHECK = _svg("M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z")
CHECK_CIRCLE = _svg("M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 "
                    "14.17l7.59-7.59L19 8l-9 9z")
X = _svg("M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z")
X_CIRCLE = _svg("M12 2C6.47 2 2 6.47 2 12s4.47 10 10 10 10-4.47 10-10S17.53 2 12 2zm5 13.59L15.59 17 12 13.41 "
                "8.41 17 7 15.59 10.59 12 7 8.41 8.41 7 12 10.59 15.59 7 17 8.41 13.41 12 17 15.59z")
PEOPLE = _svg("M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 "
              "2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5"
              "c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33"
              "-4.67-3.5-7-3.5z")
SCAN = _svg("M3 5v4h2V5h4V3H5c-1.1 0-2 .9-2 2zm2 10H3v4c0 1.1.9 2 2 2h4v-2H5v-4zm14 4h-4v2h4c1.1 0 2-.9 2-2v-4"
            "h-2v4zm0-16h-4v2h4v4h2V5c0-1.1-.9-2-2-2zM12 8c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4z")
# Focus frame with a person inside (person detection).
DETECT = _svg("M3 5v4h2V5h4V3H5c-1.1 0-2 .9-2 2zm2 10H3v4c0 1.1.9 2 2 2h4v-2H5v-4zm14 4h-4v2h4c1.1 0 2-.9 2-2v-4"
              "h-2v4zm0-16h-4v2h4v4h2V5c0-1.1-.9-2-2-2zM12 6.5a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5z"
              "M6.5 17.5c0-2.2 3.67-3.3 5.5-3.3s5.5 1.1 5.5 3.3v.5h-11v-.5z")
CLOCK = _svg("M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm4.2 14.2L11 13V7h1.5v5.2l4.5 2.7-.8 "
             "1.3z")
WARNING = _svg("M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z")
LIST = _svg("M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z")
SHIELD = _svg("M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17"
              "l6.59-6.59L18 9l-8 8z")
CAMERA_OFF = _svg("M21 6.5l-4 4V7c0-.55-.45-1-1-1H9.82L21 17.18V6.5zM3.27 2L2 3.27 4.73 6H4c-.55 0-1 .45-1 1v10"
                  "c0 .55.45 1 1 1h12c.21 0 .39-.08.54-.18L19.73 21 21 19.73 3.27 2z")
BAR_CHART = _svg("M5 9.2h3V19H5V9.2zM10.6 5h2.8v14h-2.8V5zm5.6 8H19v6h-2.8v-6z")
QR_SCAN = _svg("M9.5 6.5v3h-3v-3h3M11 5H5v6h6V5zm-1.5 9.5v3h-3v-3h3M11 13H5v6h6v-6zm6.5-6.5v3h-3v-3h3M19 5h-6v6h6"
               "V5zm-6 8h1.5v1.5H13V13zm1.5 1.5H16V16h-1.5v-1.5zM16 13h1.5v1.5H16V13zm-3 3h1.5v1.5H13V16zm1.5 1.5"
               "H16V19h-1.5v-1.5zM16 16h1.5v1.5H16V16zm1.5-1.5H19V16h-1.5v-1.5zm0 3H19V19h-1.5v-1.5zM22 7h-2V4h-3"
               "V2h5v5zm0 15v-5h-2v3h-3v2h5zM2 22h5v-2H4v-3H2v5zM2 2v5h2V4h3V2H2z")
HOME = _svg("M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z")
GEAR = _svg("M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32"
            "a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84"
            "c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87"
            "c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61"
            "l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84"
            "c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32"
            "c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 "
            "3.6-1.62 3.6-3.6 3.6z")
BOLT = _svg("M7 2v11h3v9l7-12h-4l4-8z")
CAMERA = _svg("M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11"
              "l-4 4z")
PIN = _svg("M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 0 1 0-5 "
           "2.5 2.5 0 0 1 0 5z")
FILE_TEXT = _svg("M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2z"
                 "m0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z")


@lru_cache(maxsize=256)
def data_uri(svg: str, colour: str) -> str:
    """``svg`` with its ``currentColor`` fill replaced by ``colour`` as a base64 ``data:`` URI."""
    markup = svg.replace("currentColor", colour)
    if "xmlns=" not in markup:  # required for SVG loaded as an image
        markup = markup.replace("<svg ", "<svg " + _NS, 1)
    return "data:image/svg+xml;base64," + base64.b64encode(markup.encode("utf-8")).decode("ascii")


def icon(svg: str, colour: str, size: int = 24, cls: str = "") -> str:
    """A decorative ``<img>`` rendering ``svg`` in ``colour`` at ``size`` px (survives ``st.html``)."""
    css = "tpd-icon" + (f" {cls}" if cls else "")
    size = int(size)
    return (f'<img class="{html.escape(css, quote=True)}" src="{data_uri(svg, colour)}" width="{size}" '
            f'height="{size}" alt="" aria-hidden="true" draggable="false">')
