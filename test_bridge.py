"""Hermetic tests for the bridge — no network, no real Claude. Run: pytest -q"""

import base64
import json
import struct
import zlib

import pytest
from starlette.testclient import TestClient

import server


def _png() -> str:
    raw = b"\x00" + bytes([200, 60, 60]) * 4  # 4x1 red row
    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0))
    png += ch(b"IDAT", zlib.compress(raw, 9)) + ch(b"IEND", b"")
    return base64.b64encode(png).decode()


def _body(url="https://app.example.com/x", title="X", note="n"):
    return {
        "url": url,
        "title": title,
        "note": note,
        "captured_at": "2026-06-24T09:00:00Z",
        "tiles": [_png(), _png()],
        "metadata": {
            "viewport": {"w": 1280, "h": 720, "dpr": 2},
            "scroll_height": 1840,
            "elements": [
                {"selector": "button.export", "tag": "button", "rect": [1100, 80, 120, 36],
                 "text": "Export", "tile": 0, "font_px": 13, "contrast": 2.1}
            ],
            "issues": ["resource 413 https://app.example.com/avatar"],
            "selection": {
                "text": "Cancel plan",
                "html": "<button class=\"cancel-plan\">Cancel plan</button>",
                "selector": "div.plan-actions",
                "rect": [980, 1840, 120, 36],
                "tile": 2,
                "font_px": 11,
                "contrast": 2.3,
            },
            "vitals": {"lcp_ms": 2450, "cls": 0.31, "long_tasks": 3},
            "audit": {
                "contrast": ["2.1:1 (13px) `button.export`"],
                "a11y": ["empty a: `a.icon-only`"],
                "images": ["oversized: cdn.example.com/hero.png — natural 4200px, rendered 320px"],
            },
            "meta": {"description": None, "canonical": None, "lang": "en",
                     "og_title": None, "og_image": None},
            "third_parties": [{"host": "analytics.example.net", "requests": 14, "kb": 380, "slowest_ms": 1800}],
            "legal_links": [
                {"url": "https://app.example.com/terms", "kind": "terms", "text": "Terms of Service"}
            ],
        },
        "linked": [
            {"kind": "terms", "url": "https://app.example.com/terms", "ok": True,
             "html": "<p>You owe us everything.</p>"},
            {"kind": "privacy", "url": "https://app.example.com/p.pdf", "ok": True,
             "pdfB64": base64.b64encode(b"%PDF-1.4 fake").decode()},
        ],
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Save clips under a temp dir; reset in-memory state between tests.
    monkeypatch.setattr(server, "_SAVE_ROOT", tmp_path)
    server._STORE.update({"latest": None, "history": [], "delivered": True})
    yield


@pytest.fixture(scope="session")
def client():
    # The MCP session manager can only run once per instance, so build the ASGI
    # app and enter the lifespan a single time for the whole test session.
    with TestClient(server.mcp.streamable_http_app()) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "has_latest": False}


def test_capture_persists_and_pending_fires_once(client, tmp_path):
    r = client.post("/capture", json=_body())
    assert r.json()["tiles"] == 2

    # Tiles + clip.json written to disk.
    folders = list(tmp_path.iterdir())
    assert len(folders) == 1
    assert (folders[0] / "tile00.png").exists()
    assert json.loads((folders[0] / "clip.json").read_text())["url"] == "https://app.example.com/x"

    # Linked docs written beside the tiles, with honest extensions.
    assert (folders[0] / "linked-01-terms.html").exists()
    assert (folders[0] / "linked-02-privacy.pdf").read_bytes().startswith(b"%PDF")
    meta = json.loads((folders[0] / "clip.json").read_text())
    assert meta["linked"][0]["file"] == "linked-01-terms.html"

    # /pending returns the fresh clip exactly once.
    first = client.get("/pending").json()
    assert first["fresh"] is True
    assert first["url"] == "https://app.example.com/x"
    assert first["dir"]  # path surfaced for the agent
    assert client.get("/pending").json() == {"fresh": False}


def test_no_save_env(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_SAVE", False)
    client.post("/capture", json=_body())
    assert list(tmp_path.iterdir()) == []  # nothing written
    assert server._STORE["latest"]["dir"] is None


def test_summary_surfaces_metadata():
    body = _body()
    # In the real flow _save_to_disk stamps `file` on saved linked docs.
    body["linked"][0]["file"] = "linked-01-terms.html"
    server._STORE["latest"] = {**body, "received_at": 1.0, "dir": "/tmp/x"}
    s = server._summary(server._STORE["latest"])
    assert "https://app.example.com/x" in s
    assert "button.export" in s  # selector → source mapping
    assert "413" in s  # network issue surfaced
    assert "/tmp/x" in s  # saved path
    # Instrumentation pack v2:
    assert '"Cancel plan"' in s  # user's selection as pointer
    assert "in `div.plan-actions`, tile 2, 11px, contrast 2.3:1" in s  # anchored
    assert 'selected markup: `<button class="cancel-plan">' in s  # fragment HTML
    assert "LCP 2450ms" in s and "CLS 0.31" in s  # buffered vitals
    assert "Contrast violations" in s and "2.1:1" in s  # audit first
    assert "analytics.example.net" in s  # third-party inventory
    assert "MISSING: description, og_title" in s  # head gaps
    assert "[tile 0, contrast 2.1:1]" in s  # element annotations
    assert "terms: https://app.example.com/terms" in s  # linked policy pages
    assert "saved as linked-01-terms.html" in s  # agent told where to Read them


def test_summary_accepts_legacy_string_selection():
    body = _body()
    body["metadata"]["selection"] = "Cancel plan"  # pre-anchor clips
    server._STORE["latest"] = {**body, "received_at": 1.0, "dir": None}
    assert '"Cancel plan"' in server._summary(server._STORE["latest"])


def test_pending_carries_selection_and_audit_counts(client):
    client.post("/capture", json=_body())
    p = client.get("/pending").json()
    assert p["selection"]["text"] == "Cancel plan"
    assert p["audit_counts"] == {"contrast": 1, "a11y": 1, "images": 1}


def test_slug():
    assert server._slug("Billing — Example App!") == "billing-example-app"
    assert server._slug("") == "page"


def test_cli_capture_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_SAVE", True)
    server._apply_cli(["--capture-dir", str(tmp_path / "clips")])
    assert server._SAVE_ROOT == tmp_path / "clips"
    server._apply_cli([f"--capture-dir={tmp_path / 'other'}"])
    assert server._SAVE_ROOT == tmp_path / "other"
    server._apply_cli(["--no-save"])
    assert server._SAVE is False
