"""Post one of a few preset 'clipped pages' to the bridge — stands in for the
extension so we can swap what's clipped between pulls. Usage: python post_page.py a|b"""

import base64
import json
import struct
import sys
import urllib.request
import zlib


def _png(width: int, height: int, bands: list[tuple[int, int, int]]) -> str:
    rows = []
    band_h = max(1, height // len(bands))
    for y in range(height):
        r, g, b = bands[min(y // band_h, len(bands) - 1)]
        rows.append(b"\x00" + bytes([r, g, b]) * width)
    raw = b"".join(rows)

    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


PAGES = {
    "a": {
        "url": "https://app.example.com/settings/billing",
        "title": "Billing — Example App",
        "note": "Cancel-plan button is below the fold and low-contrast.",
        "tiles": [_png(200, 120, [(220, 60, 60), (240, 200, 70), (90, 170, 90)])],
        "metadata": {"console": ["[a11y] button.cancel-plan contrast 2.3:1"]},
    },
    "b": {
        "url": "https://app.example.com/users/42/profile",
        "title": "Edit profile — Example App",
        "note": "Avatar upload silently fails over 2MB; no error shown to the user.",
        "tiles": [_png(200, 120, [(40, 40, 60), (140, 90, 200), (60, 110, 210)])],
        "metadata": {"console": ["POST /avatar 413 (Payload Too Large) at upload.js:51"]},
    },
}

page = PAGES[sys.argv[1]]
req = urllib.request.Request(
    "http://127.0.0.1:7777/capture",
    data=json.dumps(page).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as r:
    print(f"posted page '{sys.argv[1]}' ({page['url']}) ->", r.status, r.read().decode())
