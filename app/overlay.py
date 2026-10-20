"""OpenCV drawing of the detection overlay (no state, no database access).

Box colours: grey = detected only, amber = boarded / ticket verification
required, green = VALID ticket, red = failed ticket check. Counters come from
the pipeline's in-memory state, so drawing never queries the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np

GREY = (160, 160, 160)
AMBER = (0, 191, 255)
GREEN = (0, 200, 0)
RED = (0, 0, 255)
WHITE = (255, 255, 255)
LINE_COLOUR = (0, 255, 255)
_FONT = cv2.FONT_HERSHEY_SIMPLEX

Point = Tuple[int, int]


@dataclass
class OverlayState:
    people: Sequence = ()
    line: Optional[Tuple[Point, Point]] = None
    status_by_track: Mapping[int, str] = field(default_factory=dict)  # Track ID -> boarding status
    boardings: int = 0
    valid: int = 0
    needs_check: int = 0
    messages: Sequence[str] = ()


def colour_for(status: Optional[str]) -> Tuple[int, int, int]:
    if status is None:
        return GREY
    if status == "VALID":
        return GREEN
    if status == "VERIFICATION_REQUIRED":
        return AMBER
    return RED


def _label(status: Optional[str]) -> str:
    if status is None:
        return ""
    if status == "VERIFICATION_REQUIRED":
        return " Verification required"
    return f" {status.replace('_', ' ').title()}"


def _text(frame: np.ndarray, text: str, org: Point, colour, scale: float = 0.5, thickness: int = 1) -> None:
    cv2.putText(frame, text, org, _FONT, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, org, _FONT, scale, colour, thickness, cv2.LINE_AA)


def draw_overlay(frame: np.ndarray, state: OverlayState) -> np.ndarray:
    """Draw the entry line, person boxes, counters and recent messages onto ``frame`` (in place)."""
    h, _ = frame.shape[:2]
    if state.line is not None:
        p1, p2 = state.line
        cv2.line(frame, p1, p2, LINE_COLOUR, 2)
        _text(frame, "ENTRY LINE", (p1[0] + 5, max(15, p1[1] - 8)), LINE_COLOUR)
    for person in state.people:
        status = state.status_by_track.get(int(person.track_id))
        colour = colour_for(status)
        x1, y1, x2, y2 = (int(v) for v in person.bbox)
        cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)
        _text(frame, f"ID {person.track_id} {person.confidence:.2f}{_label(status)}", (x1, max(15, y1 - 6)), colour)
    summary = f"Boardings {state.boardings} | Valid {state.valid} | Needs check {state.needs_check}"
    _text(frame, summary, (10, h - 12), WHITE, 0.55, 1)
    for i, message in enumerate(reversed(list(state.messages)[-3:])):
        _text(frame, message[:90], (10, 22 + 20 * i), WHITE)
    return frame
