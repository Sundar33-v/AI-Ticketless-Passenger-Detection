"""Virtual entry-line crossing detection.

Consumes tracked people (anything with ``track_id`` and ``bbox``) and emits an
:class:`EntryEvent` when a Track ID's anchor point crosses a configurable
horizontal or vertical line.

Duplicate protection:
  * A Track ID emits at most ONE event per direction for its lifetime
    (until :meth:`EntryLineDetector.reset`).
  * After any event, the same Track ID is ignored for ``cooldown_seconds``,
    which suppresses jitter "bounces" back across the line.
  * A hysteresis band (``hysteresis_px``) around the line means a point must
    fully leave the band on the other side before a crossing counts.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

HORIZONTAL = "horizontal"
VERTICAL = "vertical"

TOP_TO_BOTTOM = "top_to_bottom"
BOTTOM_TO_TOP = "bottom_to_top"
LEFT_TO_RIGHT = "left_to_right"
RIGHT_TO_LEFT = "right_to_left"
BOTH = "both"

_VALID_DIRECTIONS = {
    HORIZONTAL: {TOP_TO_BOTTOM, BOTTOM_TO_TOP, BOTH},
    VERTICAL: {LEFT_TO_RIGHT, RIGHT_TO_LEFT, BOTH},
}
_ANCHORS = {"bottom_center", "center"}


@dataclass(frozen=True)
class EntryEvent:
    track_id: int
    direction: str
    timestamp: float  # Unix epoch seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EntryLineConfig:
    """Configuration of the virtual entry line.

    orientation: "horizontal" (line at y=position) or "vertical" (x=position).
    position: line coordinate in pixels, or a 0..1 fraction if ``normalized``.
    span_start / span_end: optional limits of the line along its length
        (x-range for horizontal, y-range for vertical); same units as position.
    entry_direction: which crossing direction(s) produce events; "both" emits
        events for either direction.
    anchor: bbox point used for crossing tests ("bottom_center" or "center").
    """

    orientation: str = HORIZONTAL
    position: float = 0.5
    normalized: bool = True
    span_start: Optional[float] = None
    span_end: Optional[float] = None
    entry_direction: str = TOP_TO_BOTTOM
    cooldown_seconds: float = 2.0
    hysteresis_px: float = 5.0
    anchor: str = "bottom_center"
    # Forget per-track side state for tracks not updated for this long.
    track_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.orientation = str(self.orientation).lower()
        self.entry_direction = str(self.entry_direction).lower()
        if self.orientation not in _VALID_DIRECTIONS:
            raise ValueError(f"orientation must be 'horizontal' or 'vertical', got {self.orientation!r}")
        if self.entry_direction not in _VALID_DIRECTIONS[self.orientation]:
            raise ValueError(
                f"entry_direction {self.entry_direction!r} invalid for {self.orientation} line; "
                f"expected one of {sorted(_VALID_DIRECTIONS[self.orientation])}"
            )
        if self.anchor not in _ANCHORS:
            raise ValueError(f"anchor must be one of {sorted(_ANCHORS)}, got {self.anchor!r}")
        if self.cooldown_seconds < 0 or self.hysteresis_px < 0:
            raise ValueError("cooldown_seconds and hysteresis_px must be >= 0")
        if self.normalized:
            for name in ("position", "span_start", "span_end"):
                v = getattr(self, name)
                if v is not None and not 0.0 <= float(v) <= 1.0:
                    raise ValueError(f"{name} must be within [0, 1] when normalized=True")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "EntryLineConfig":
        data = dict(data or {})
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass
class _TrackLineState:
    side: int  # -1 before line (top/left), +1 after line (bottom/right), 0 unknown
    last_update: float
    last_event_time: Optional[float] = None


class EntryLineDetector:
    def __init__(self, config: Optional[EntryLineConfig] = None) -> None:
        self.config = config or EntryLineConfig()
        self._states: Dict[int, _TrackLineState] = {}
        self._emitted: Dict[int, Set[str]] = {}

    # --------------------------------------------------------------- geometry
    def _resolve(self, value: Optional[float], axis_len: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if self.config.normalized:
            if axis_len is None:
                raise ValueError("frame_size is required when the entry line is normalized")
            return float(value) * float(axis_len)
        return float(value)

    def _line_geometry(self, frame_size: Optional[Tuple[int, int]]) -> Tuple[float, Optional[float], Optional[float]]:
        """Return (line_coord, span_lo, span_hi) in pixels. frame_size = (width, height)."""
        w, h = (frame_size if frame_size is not None else (None, None))
        if self.config.orientation == HORIZONTAL:
            pos_len, span_len = h, w
        else:
            pos_len, span_len = w, h
        pos = self._resolve(self.config.position, pos_len)
        lo = self._resolve(self.config.span_start, span_len)
        hi = self._resolve(self.config.span_end, span_len)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return pos, lo, hi

    def line_endpoints(self, frame_size: Tuple[int, int]) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """Pixel endpoints of the line, e.g. for drawing an overlay."""
        w, h = frame_size
        pos, lo, hi = self._line_geometry(frame_size)
        if self.config.orientation == HORIZONTAL:
            lo, hi = (0 if lo is None else lo), (w if hi is None else hi)
            return (int(lo), int(pos)), (int(hi), int(pos))
        lo, hi = (0 if lo is None else lo), (h if hi is None else hi)
        return (int(pos), int(lo)), (int(pos), int(hi))

    def _anchor_point(self, bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        return (cx, y2) if self.config.anchor == "bottom_center" else (cx, (y1 + y2) / 2.0)

    def _side(self, coord: float, line: float) -> int:
        m = self.config.hysteresis_px
        if coord < line - m:
            return -1
        if coord > line + m:
            return 1
        return 0  # inside hysteresis band

    def _direction(self, prev_side: int, new_side: int) -> str:
        forward = new_side > prev_side  # moving towards increasing y / x
        if self.config.orientation == HORIZONTAL:
            return TOP_TO_BOTTOM if forward else BOTTOM_TO_TOP
        return LEFT_TO_RIGHT if forward else RIGHT_TO_LEFT

    # ----------------------------------------------------------------- update
    def update(
        self,
        tracks: Iterable[Any],
        timestamp: Optional[float] = None,
        frame_size: Optional[Tuple[int, int]] = None,
    ) -> List[EntryEvent]:
        """Process one frame of tracks and return any new entry events.

        ``tracks``: objects with ``track_id`` and ``bbox`` (x1, y1, x2, y2)
        attributes, or mappings with those keys.
        ``frame_size``: (width, height); required when the line is normalized.
        """
        ts = time.time() if timestamp is None else float(timestamp)
        line, lo, hi = self._line_geometry(frame_size)
        events: List[EntryEvent] = []

        for track in tracks:
            if isinstance(track, Mapping):
                tid, bbox = track["track_id"], track["bbox"]
            else:
                tid, bbox = track.track_id, track.bbox
            if tid is None:
                continue
            event = self._update_point(int(tid), self._anchor_point(tuple(bbox)), ts, line, lo, hi)
            if event is not None:
                events.append(event)

        self._prune(ts)
        return events

    def update_point(
        self,
        track_id: int,
        x: float,
        y: float,
        timestamp: Optional[float] = None,
        frame_size: Optional[Tuple[int, int]] = None,
    ) -> Optional[EntryEvent]:
        """Process a single anchor point directly (no bbox)."""
        ts = time.time() if timestamp is None else float(timestamp)
        line, lo, hi = self._line_geometry(frame_size)
        return self._update_point(int(track_id), (float(x), float(y)), ts, line, lo, hi)

    def _update_point(
        self,
        tid: int,
        point: Tuple[float, float],
        ts: float,
        line: float,
        lo: Optional[float],
        hi: Optional[float],
    ) -> Optional[EntryEvent]:
        x, y = point
        coord, along = (y, x) if self.config.orientation == HORIZONTAL else (x, y)
        side = self._side(coord, line)

        state = self._states.get(tid)
        if state is None:
            self._states[tid] = _TrackLineState(side=side, last_update=ts)
            return None
        state.last_update = ts

        if side == 0 or side == state.side:
            return None  # inside band or no change
        prev_side, state.side = state.side, side
        if prev_side == 0:
            return None  # first definite side observed; not a crossing

        # Only count crossings within the line segment's extent.
        if (lo is not None and along < lo) or (hi is not None and along > hi):
            return None

        direction = self._direction(prev_side, side)
        if self.config.entry_direction != BOTH and direction != self.config.entry_direction:
            return None
        if state.last_event_time is not None and ts - state.last_event_time < self.config.cooldown_seconds:
            return None
        emitted = self._emitted.setdefault(tid, set())
        if direction in emitted:
            return None  # duplicate for this Track ID

        emitted.add(direction)
        state.last_event_time = ts
        return EntryEvent(track_id=tid, direction=direction, timestamp=ts)

    # ------------------------------------------------------------------ state
    def _prune(self, now: float) -> None:
        timeout = self.config.track_timeout_seconds
        stale = [t for t, s in self._states.items() if now - s.last_update > timeout]
        for tid in stale:
            del self._states[tid]
        # _emitted is intentionally kept so a returning Track ID is never double counted.

    def has_entered(self, track_id: int) -> bool:
        return bool(self._emitted.get(int(track_id)))

    def reset(self) -> None:
        self._states.clear()
        self._emitted.clear()
