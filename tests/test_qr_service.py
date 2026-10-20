"""Tests for app.qr_service (tokens, payload parsing, PNG round-trip via OpenCV)."""

from __future__ import annotations

import hashlib
import json
import re

import numpy as np
import pytest

from app.qr_service import (
    MAX_PAYLOAD_LENGTH,
    TOKEN_PREFIX,
    ParsedCredential,
    QRDetection,
    QRScanner,
    demo_token,
    encode_payload,
    hash_token,
    new_token,
    parse_payload,
    write_qr_png,
)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")


def qr_frame(payload: str) -> np.ndarray:
    import qrcode

    qr = qrcode.QRCode(box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return np.array(img, dtype=np.uint8)[:, :, ::-1].copy()  # RGB -> BGR


# ------------------------------------------------------------------ tokens
def test_new_token_length_charset_and_uniqueness():
    tokens = {new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(TOKEN_RE.match(t) for t in tokens)


def test_hash_token_is_sha256_hex():
    digest = hash_token("abc")
    assert digest == hashlib.sha256(b"abc").hexdigest()
    assert len(digest) == 64 and re.fullmatch(r"[0-9a-f]{64}", digest)


def test_demo_token_deterministic_and_32_chars():
    a = demo_token(20260115, "DEMO-TKT-001")
    assert a == demo_token(20260115, "DEMO-TKT-001")
    assert TOKEN_RE.match(a)
    assert a != demo_token(20260115, "DEMO-TKT-002") and a != demo_token(1, "DEMO-TKT-001")
    assert parse_payload(encode_payload(a)).wellformed


# ----------------------------------------------------------------- parsing
def test_prefixed_and_bare_token_accepted():
    token = new_token()
    for text in (encode_payload(token), token, f"  {TOKEN_PREFIX}{token}\n", token.encode()):
        parsed = parse_payload(text)
        assert parsed == ParsedCredential(token=token, wellformed=True, token_hash=hash_token(token))


def test_token_length_bounds():
    assert parse_payload("a" * 64).wellformed
    assert not parse_payload("a" * 31).wellformed
    assert not parse_payload("a" * 65).wellformed


@pytest.mark.parametrize("payload", [
    json.dumps({"ticket_id": "TKT-0001", "name": "Passenger"}),
    "TKT-0001",
    "TICKET:TKT-0001",
    "TPD1:short",
    "TPD2:" + "a" * 32,
    "a" * 31 + "!",
    "",
    None,
])
def test_unrecognised_payloads(payload):
    parsed = parse_payload(payload)
    assert parsed.wellformed is False and parsed.token is None
    raw = "" if payload is None else payload
    assert parsed.token_hash == hash_token(raw)


def test_oversized_payload_rejected():
    text = TOKEN_PREFIX + "a" * 40 + " " * (MAX_PAYLOAD_LENGTH)
    assert len(text) > MAX_PAYLOAD_LENGTH
    assert parse_payload(text).wellformed is False
    assert parse_payload("x" * 5000).token is None


# ---------------------------------------------------------------- decoding
def test_png_round_trip_path_and_bytes(tmp_path):
    payload = encode_payload(new_token())
    path = write_qr_png(payload, tmp_path / "nested" / "dir" / "ticket.png")
    assert path.is_file()
    scanner = QRScanner()
    from_path = scanner.decode_image(path)
    from_bytes = scanner.decode_image(path.read_bytes())
    assert [d.payload for d in from_path] == [payload]
    assert [d.payload for d in from_bytes] == [payload]
    assert parse_payload(from_path[0].payload).wellformed


def test_non_ascii_path(tmp_path):
    payload = encode_payload(new_token())
    path = write_qr_png(payload, tmp_path / "टिकट_bilet_é" / "qr.png")
    assert [d.payload for d in QRScanner().decode_image(path)] == [payload]


@pytest.mark.parametrize("token", ["L-QIIiOPCVN-N-_e3nUwPbvniy7nJh0L", "1oiuajFKBdZCUVZi6Jcci8SepfBsaSib",
                                   "mCsBHLHIDCsXvX7b9cgAtgmyuVT6muFC"])
def test_decode_image_reads_codes_the_default_detector_misses(tmp_path, token):
    # Generated PNGs of these tokens are not decoded by cv2.QRCodeDetector alone.
    png = write_qr_png(encode_payload(token), tmp_path / "t.png")
    assert [d.payload for d in QRScanner().decode_image(png)] == [encode_payload(token)]


def test_decode_frame_returns_points():
    [det] = QRScanner().decode_frame(qr_frame("TPD1:" + "b" * 32))
    assert isinstance(det, QRDetection) and det.payload == "TPD1:" + "b" * 32
    assert det.points is not None and len(det.points) == 4


def test_grayscale_frame():
    gray = qr_frame("TKT-0002")[:, :, 0].copy()
    assert [d.payload for d in QRScanner().decode_frame(gray)] == ["TKT-0002"]


def test_blank_and_bad_frames():
    scanner = QRScanner()
    assert scanner.decode_frame(np.full((200, 200, 3), 255, np.uint8)) == []
    assert scanner.decode_frame(np.zeros((0, 0, 3), np.uint8)) == []
    assert scanner.decode_frame(None) == []
    assert scanner.decode_frame(np.full((50, 50, 3), 300.0)) == []  # non-uint8 is clipped


def test_unreadable_image_raises(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    scanner = QRScanner()
    for source in (bad, b"not an image", b"", tmp_path / "missing.png"):
        with pytest.raises(ValueError, match="Could not read image"):
            scanner.decode_image(source)
