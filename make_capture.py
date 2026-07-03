"""Simulate the browser extension: build a sample capture (two banded PNG
'scroll tiles' + realistic page metadata) and POST it to the bridge.

Pure stdlib — no Pillow needed. Run while server.py is up:
    python make_capture.py
"""

from __future__ import annotations

import base64
import json
import struct
import urllib.request
import zlib


def _png(width: int, height: int, bands: list[tuple[int, int, int]]) -> bytes:
    """Encode an RGB PNG split into equal horizontal colour bands."""
    rows = []
    band_h = max(1, height // len(bands))
    for y in range(height):
        r, g, b = bands[min(y // band_h, len(bands) - 1)]
        rows.append(b"\x00" + bytes([r, g, b]) * width)  # filter byte 0 + pixels
    raw = b"".join(rows)

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode()


# Two distinct "tiles" — top of page (warm bands) and a scrolled-down section.
tile_top = _b64(_png(240, 160, [(220, 60, 60), (240, 200, 70), (90, 170, 90)]))
tile_low = _b64(_png(240, 160, [(60, 110, 210), (140, 90, 200), (40, 40, 60)]))

payload = {
    "url": "https://app.example.com/settings/billing",
    "title": "Billing — Example App",
    "note": "The 'Cancel plan' button is hidden below the fold and low-contrast; "
    "the invoice table overflows on narrow viewports.",
    "captured_at": "2026-06-21T10:15:00Z",
    "tiles": [tile_top, tile_low],
    "metadata": {
        "viewport": {"w": 1280, "h": 720, "dpr": 2},
        "scroll_height": 2200,
        "console": [
            "TypeError: Cannot read properties of undefined (reading 'amount') at invoice.js:88",
            "[a11y] button.cancel-plan has insufficient contrast ratio 2.3:1",
        ],
        "elements": [
            {"selector": "button.cancel-plan", "rect": [980, 1840, 120, 36], "text": "Cancel plan"},
            {"selector": "table.invoices", "rect": [40, 420, 1320, 600], "text": "Date Amount Status"},
            {"selector": "a.update-payment", "rect": [40, 320, 180, 40], "text": "Update payment method"},
        ],
    },
}

req = urllib.request.Request(
    "http://127.0.0.1:7777/capture",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as r:
    print("POST /capture ->", r.status, r.read().decode())
