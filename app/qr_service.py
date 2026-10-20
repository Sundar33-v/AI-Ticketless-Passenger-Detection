"""QR ticket credentials: opaque tokens, payload parsing, PNG generation and decoding.

A ticket QR code carries only an opaque random token (``TPD1:<token>``). It
never carries passenger details; the token is looked up by its SHA-256 hash in
the database, which is the only source of truth. The database stores only the
hash, never the plaintext token.

Decoding uses OpenCV's ``QRCodeDetector`` only (no extra native decoder library).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

import cv2
import numpy as np

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "TPD1:"
MAX_PAYLOAD_LENGTH = 512
_PAYLOAD_RE = re.compile(r"^(?:TPD1:)?([A-Za-z0-9_-]{32,64})$")

Points = Tuple[Tuple[float, float], ...]


# ------------------------------------------------------------------ tokens
def new_token() -> str:
    """A fresh 32-character URL-safe random token (192 bits)."""
    return secrets.token_urlsafe(24)


def demo_token(seed: int, ticket_id: str) -> str:
    """Deterministic 32-character token for synthetic demo tickets only."""
    digest = hmac.new(f"tpd-demo-{seed}".encode(), ticket_id.encode(), "sha256").digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()[:32]


def hash_token(token: str) -> str:
    """SHA-256 hex digest (64 chars); the only form in which tokens are stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def encode_payload(token: str) -> str:
    return TOKEN_PREFIX + token


@dataclass(frozen=True)
class ParsedCredential:
    """Result of parsing a QR payload. ``token`` is None when not well formed."""

    token: Optional[str]
    wellformed: bool
    token_hash: str


def parse_payload(text: Union[str, bytes, None]) -> ParsedCredential:
    """Accept ``TPD1:<token>`` or a bare token (``[A-Za-z0-9_-]{32,64}``).

    Anything else (JSON, plain ticket IDs, > 512 chars) is unrecognised; its
    ``token_hash`` is the SHA-256 of the raw payload so attempts can be logged
    without storing the content.
    """
    if text is None:
        raw = ""
    elif isinstance(text, bytes):
        raw = text.decode("utf-8", errors="replace")
    else:
        raw = str(text)
    stripped = raw.strip()
    if len(raw) <= MAX_PAYLOAD_LENGTH:
        match = _PAYLOAD_RE.match(stripped)
        if match:
            token = match.group(1)
            return ParsedCredential(token=token, wellformed=True, token_hash=hash_token(token))
    return ParsedCredential(token=None, wellformed=False, token_hash=hash_token(raw))


def write_qr_png(payload: str, path: Union[str, Path]) -> Path:
    """Write ``payload`` as a QR PNG (error correction M, box 10, border 4)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(str(out))
    return out


# ---------------------------------------------------------------- decoding
@dataclass(frozen=True)
class QRDetection:
    payload: str
    points: Optional[Points] = None  # QR corner polygon in frame coordinates


def _polygon(pts) -> Optional[Points]:
    if pts is None:
        return None
    return tuple((float(x), float(y)) for x, y in np.asarray(pts).reshape(-1, 2))


class QRScanner:
    """OpenCV QR decoder for camera frames and uploaded images."""

    def __init__(self) -> None:
        self._detector = cv2.QRCodeDetector()
        self._aruco_detector = None  # created on first use (still images only)

    def decode_frame(self, frame: np.ndarray, *, thorough: bool = False) -> List[QRDetection]:
        """Decode all QR codes in a BGR/grayscale frame (deduplicated by payload).

        ``thorough=True`` adds a slower last-resort decoder; it is used for
        uploaded/still images, not for the per-frame camera path.
        """
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            return []
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        try:
            detections = self._decode(frame, thorough)
        except cv2.error as exc:
            logger.debug("QR decode failed: %s", exc)
            return []
        unique: List[QRDetection] = []
        seen = set()
        for det in detections:
            if det.payload not in seen:
                seen.add(det.payload)
                unique.append(det)
        return unique

    def _decode(self, frame: np.ndarray, thorough: bool = False) -> List[QRDetection]:
        results: List[QRDetection] = []
        try:
            ok, decoded, points, _ = self._detector.detectAndDecodeMulti(frame)
        except cv2.error as exc:
            logger.debug("OpenCV multi-decode failed: %s", exc)
            ok, decoded, points = False, (), None
        if ok:
            for i, text in enumerate(decoded):
                if text:
                    pts = points[i] if points is not None and i < len(points) else None
                    results.append(QRDetection(text, _polygon(pts)))
        if not results:  # the single-code decoder is sometimes more tolerant
            text, pts, _ = self._detector.detectAndDecode(frame)
            if text:
                results.append(QRDetection(text, _polygon(pts)))
        if not results and thorough:
            # The default detector misses a small share (~1%) of clean generated
            # codes; the ArUco-based detector decodes those.
            if self._aruco_detector is None:
                self._aruco_detector = cv2.QRCodeDetectorAruco()
            text, pts, _ = self._aruco_detector.detectAndDecode(frame)
            if text:
                results.append(QRDetection(text, _polygon(pts)))
        return results

    def decode_image(self, source: Union[str, Path, bytes]) -> List[QRDetection]:
        """Decode QR codes from an image file path or raw image bytes.

        Uses ``np.fromfile``/``np.frombuffer`` + ``cv2.imdecode`` so non-ASCII
        Windows paths work. Raises ``ValueError("Could not read image")``.
        """
        try:
            if isinstance(source, (bytes, bytearray, memoryview)):
                data = np.frombuffer(bytes(source), dtype=np.uint8)
            else:
                data = np.fromfile(str(source), dtype=np.uint8)
        except OSError as exc:
            raise ValueError("Could not read image") from exc
        try:
            image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        except cv2.error:
            image = None
        if image is None:
            raise ValueError("Could not read image")
        return self.decode_frame(image, thorough=True)
