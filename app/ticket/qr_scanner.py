"""Read ticket IDs from QR codes in camera frames or image files.

Supported QR payloads (see ``extract_ticket_id``):

* plain ID:            ``TKT-0001``
* prefixed:            ``TICKET:TKT-0001``
* JSON:                ``{"ticket_id": "TKT-0001", ...}``

Decoding uses OpenCV's ``QRCodeDetector``; pyzbar is used as a fallback when
installed and its native zbar library loads. This module only reads QR codes -
it does no face or person identification.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple, Union

import numpy as np

if TYPE_CHECKING:
    from app.ticket.ticket_validator import TicketValidator, ValidationResult

logger = logging.getLogger(__name__)

TICKET_PREFIX = "TICKET:"
MAX_PAYLOAD_LENGTH = 1024
BACKENDS = ("auto", "opencv", "pyzbar")


def extract_ticket_id(payload: Union[str, bytes, None]) -> Optional[str]:
    """Pull the ticket ID candidate out of a QR payload.

    Returns the (stripped, un-normalized) candidate, or None if the payload is
    empty/oversized. Well-formedness is judged later by the validator.
    """
    if payload is None:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    text = payload.strip()
    if not text or len(text) > MAX_PAYLOAD_LENGTH:
        return None

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        value = data.get("ticket_id") if isinstance(data, dict) else None
        return str(value).strip() if value is not None else None

    if text.upper().startswith(TICKET_PREFIX):
        return text[len(TICKET_PREFIX):].strip() or None
    return text


@dataclass(frozen=True)
class QRDetection:
    payload: str
    ticket_id: Optional[str]
    points: Optional[Tuple[Tuple[float, float], ...]] = None  # QR corner polygon in the frame
    backend: str = "opencv"


class QRScanner:
    def __init__(self, backend: str = "auto") -> None:
        if backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}, got {backend!r}")
        self.backend = backend
        self._cv_detector = None
        self._pyzbar_decode = None
        self._pyzbar_checked = False

    # ---------------------------------------------------------------- backends
    def _opencv(self):
        if self._cv_detector is None:
            import cv2

            self._cv_detector = cv2.QRCodeDetector()
        return self._cv_detector

    def _pyzbar(self):
        if not self._pyzbar_checked:
            self._pyzbar_checked = True
            try:
                from pyzbar import pyzbar

                self._pyzbar_decode = pyzbar.decode
            except (ImportError, OSError) as exc:  # OSError: zbar DLL missing
                logger.debug("pyzbar unavailable: %s", exc)
        return self._pyzbar_decode

    def _decode_opencv(self, frame: np.ndarray) -> List[QRDetection]:
        detector = self._opencv()
        results: List[QRDetection] = []
        try:
            ok, decoded, points, _ = detector.detectAndDecodeMulti(frame)
        except Exception as exc:  # cv2.error on odd inputs
            logger.debug("OpenCV multi-decode failed: %s", exc)
            ok, decoded, points = False, (), None
        if ok:
            for i, text in enumerate(decoded):
                if not text:
                    continue
                pts = None
                if points is not None and i < len(points):
                    pts = tuple((float(x), float(y)) for x, y in np.asarray(points[i]).reshape(-1, 2))
                results.append(QRDetection(text, extract_ticket_id(text), pts, "opencv"))
        if not results:
            # Single-code decoder is sometimes more tolerant than the multi one.
            try:
                text, pts, _ = detector.detectAndDecode(frame)
            except Exception as exc:
                logger.debug("OpenCV decode failed: %s", exc)
                text, pts = "", None
            if text:
                poly = (
                    tuple((float(x), float(y)) for x, y in np.asarray(pts).reshape(-1, 2))
                    if pts is not None
                    else None
                )
                results.append(QRDetection(text, extract_ticket_id(text), poly, "opencv"))
        return results

    def _decode_pyzbar(self, frame: np.ndarray) -> List[QRDetection]:
        decode = self._pyzbar()
        if decode is None:
            return []
        results = []
        for obj in decode(frame):
            if getattr(obj, "type", "QRCODE") != "QRCODE":
                continue
            text = obj.data.decode("utf-8", errors="replace")
            pts = tuple((float(p.x), float(p.y)) for p in obj.polygon) if obj.polygon else None
            results.append(QRDetection(text, extract_ticket_id(text), pts, "pyzbar"))
        return results

    # ---------------------------------------------------------------- public API
    def decode_frame(self, frame: np.ndarray) -> List[QRDetection]:
        """Decode all QR codes in a BGR/grayscale frame (deduplicated by payload)."""
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            return []
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)

        detections: List[QRDetection] = []
        if self.backend in ("auto", "opencv"):
            detections = self._decode_opencv(frame)
        if not detections and self.backend in ("auto", "pyzbar"):
            detections = self._decode_pyzbar(frame)

        unique, seen = [], set()
        for det in detections:
            if det.payload not in seen:
                seen.add(det.payload)
                unique.append(det)
        return unique

    def scan_image(self, path: Union[str, Path]) -> List[QRDetection]:
        """Decode QR codes from an image file (handles non-ASCII Windows paths)."""
        import cv2

        data = np.fromfile(str(path), dtype=np.uint8)
        frame = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if frame is None:
            raise ValueError(f"Could not read image: {path}")
        return self.decode_frame(frame)

    def read_ticket_ids(self, frame: np.ndarray) -> List[str]:
        return [d.ticket_id for d in self.decode_frame(frame) if d.ticket_id]

    def scan_and_validate(
        self, frame: np.ndarray, validator: "TicketValidator", **validate_kwargs: Any
    ) -> List[Tuple[QRDetection, "ValidationResult"]]:
        """Decode QR codes and validate each one (kwargs go to ``validator.validate``)."""
        return [
            (det, validator.validate(det.ticket_id, **validate_kwargs))
            for det in self.decode_frame(frame)
        ]


def scan_images(paths: Sequence[Union[str, Path]], backend: str = "auto") -> List[QRDetection]:
    scanner = QRScanner(backend)
    out: List[QRDetection] = []
    for path in paths:
        out.extend(scanner.scan_image(path))
    return out
