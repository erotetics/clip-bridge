"""Clip → Claude Code bridge.

One local process, two faces:
  - HTTP POST /capture     ← the browser extension sends a clipped page here
  - MCP (streamable-http)  ← Claude Code connects and pulls the capture in

The capture (scroll-tile screenshots + page metadata + your annotation) is held
in memory; Claude Code calls `get_latest_capture` to bring it into the session.

Run:   python server.py        (listens on 127.0.0.1:7777, MCP at /mcp)
Wire:  claude mcp add --transport http capture http://localhost:7777/mcp
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("capture", host="127.0.0.1", port=7777)

# Opt-in "seamless" mode: when a clip arrives, fire off a Claude Code agent that
# pulls it and reviews it — no human prompt. Set CLIP_AUTO_REVIEW=1 to enable.
# (MCP itself is pull-only; it can't push a turn into a live session. This makes
# the *clip* the trigger instead.)
AUTO_REVIEW = os.environ.get("CLIP_AUTO_REVIEW") == "1"
_REVIEWS = pathlib.Path(__file__).parent / "reviews"
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


async def _auto_review() -> None:
    claude = shutil.which("claude")
    if not claude:
        return
    _REVIEWS.mkdir(exist_ok=True)
    cfg = '{"mcpServers":{"capture":{"type":"http","url":"http://127.0.0.1:7777/mcp"}}}'
    proc = await asyncio.create_subprocess_exec(
        claude, "-p",
        "A page was just clipped from the browser. Call get_latest_capture, then "
        "write a terse punch-list (max 3 bullets); each bullet = the problem + the "
        "file or CSS selector to fix. No preamble.",
        "--model", "haiku", "--max-turns", "15",
        "--mcp-config", cfg, "--allowedTools", "mcp__capture__get_latest_capture",
        cwd=str(_REPO_ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    (_REVIEWS / f"review-{int(time.time())}.md").write_bytes(out or b"(no output)")

# Latest capture + a small rolling history. In-memory by design: this is a
# local, ephemeral dev tool — nothing leaves the machine.
_STORE: dict[str, Any] = {"latest": None, "history": [], "delivered": True}

# Persist each clip to disk ("files on the computer"): tiles as PNGs + clip.json.
# On by default to bridge/captures/; CLIP_CAPTURE_DIR overrides, CLIP_NO_SAVE=1 off.
_SAVE = os.environ.get("CLIP_NO_SAVE") != "1"
_SAVE_ROOT = pathlib.Path(os.environ.get("CLIP_CAPTURE_DIR") or pathlib.Path(__file__).parent / "captures")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:50] or "page"


def _save_to_disk(cap: dict) -> str | None:
    """Write a clip's tiles + metadata to a timestamped folder. Returns the path
    so Claude Code can read full-res tiles by path (MCP may downscale)."""
    if not _SAVE:
        return None
    stamp = int(float(cap.get("received_at", 0)))
    folder = _SAVE_ROOT / f"{stamp}-{_slug(cap.get('title', ''))}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for i, b64 in enumerate(cap.get("tiles", [])):
            (folder / f"tile{i:02d}.png").write_bytes(base64.b64decode(b64))
        # Linked policy documents (terms/privacy/… incl. PDFs): written to disk
        # so the agent can Read them by path; heavy payloads dropped from memory.
        for i, doc in enumerate(cap.get("linked") or []):
            if not doc.get("ok"):
                continue
            stem = f"linked-{i + 1:02d}-{doc.get('kind', 'doc')}"
            if doc.get("pdfB64"):
                (folder / f"{stem}.pdf").write_bytes(base64.b64decode(doc["pdfB64"]))
                doc["file"] = f"{stem}.pdf"
            elif doc.get("html"):
                (folder / f"{stem}.html").write_text(doc["html"])
                doc["file"] = f"{stem}.html"
            doc.pop("html", None)
            doc.pop("pdfB64", None)
        meta = {
            k: cap.get(k)
            for k in ("url", "title", "note", "captured_at", "metadata", "linked")
        }
        (folder / "clip.json").write_text(json.dumps(meta, indent=2))
    except Exception:
        return None
    return str(folder)


@mcp.custom_route("/capture", methods=["POST"])
async def capture(request: Request) -> JSONResponse:
    """The extension POSTs a clipped page here.

    Body: {url, title, note?, captured_at?, tiles: [base64-png...], metadata?}
    """
    data = await request.json()
    data.setdefault("received_at", time.time())
    data["dir"] = _save_to_disk(data)  # persist tiles + metadata to disk
    _STORE["latest"] = data
    _STORE["delivered"] = False  # a fresh clip the session hook hasn't surfaced yet
    _STORE["history"].append(
        {k: data.get(k) for k in ("url", "title", "note", "received_at")}
    )
    _STORE["history"] = _STORE["history"][-50:]
    if AUTO_REVIEW:
        asyncio.create_task(_auto_review())  # fire-and-forget; POST returns now
    return JSONResponse(
        {"ok": True, "tiles": len(data.get("tiles", [])), "auto_review": AUTO_REVIEW}
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "has_latest": _STORE["latest"] is not None})


@mcp.custom_route("/pending", methods=["GET"])
async def pending(request: Request) -> JSONResponse:
    """The session hook polls this. Returns a fresh clip's metadata exactly once
    (marks it delivered) so it surfaces in the session the first time, not on
    every prompt. Text only — screenshots stay behind the MCP tool."""
    cap = _STORE["latest"]
    if not cap or _STORE.get("delivered"):
        return JSONResponse({"fresh": False})
    _STORE["delivered"] = True
    md = cap.get("metadata", {}) or {}
    audit = md.get("audit") or {}
    return JSONResponse(
        {
            "fresh": True,
            "url": cap.get("url"),
            "title": cap.get("title"),
            "note": cap.get("note"),
            "selection": md.get("selection"),
            "dir": cap.get("dir"),
            "console": md.get("console") or md.get("issues") or [],
            "audit_counts": {k: len(v) for k, v in audit.items() if v},
            "elements": md.get("elements", []),
            "tiles": len(cap.get("tiles", [])),
        }
    )


def _summary(cap: dict) -> str:
    md = cap.get("metadata", {}) or {}
    lines = [
        "# Latest clipped page",
        f"- url: {cap.get('url')}",
        f"- title: {cap.get('title')}",
    ]
    if cap.get("note"):
        lines.append(f"- note (your annotation): {cap['note']}")
    sel = md.get("selection")
    if sel:
        if isinstance(sel, str):  # pre-anchor clips sent a bare string
            lines.append(f'- selected text (my pointer — focus here): "{sel}"')
        else:
            bits = []
            if sel.get("selector"):
                bits.append(f"in `{sel['selector']}`")
            if sel.get("tile") is not None:
                bits.append(f"tile {sel['tile']}")
            if sel.get("font_px"):
                bits.append(f"{sel['font_px']}px")
            if sel.get("contrast") is not None:
                bits.append(f"contrast {sel['contrast']}:1")
            suffix = f" ({', '.join(bits)})" if bits else ""
            lines.append(
                f'- selected text (my pointer — focus here): "{sel.get("text", "")}"{suffix}'
            )
            if sel.get("html"):
                lines.append(f"  selected markup: `{sel['html'][:400]}`")
    if md.get("viewport"):
        lines.append(f"- viewport: {md['viewport']}")
    if cap.get("dir"):
        lines.append(f"- saved to: {cap['dir']} (read tileNN.png for full resolution)")

    region = md.get("region")
    if region:
        lines.append(
            f"- MARKED REGION at page rect {region.get('page_rect')} — the user drew a box "
            "around this area; the FIRST image below is the cropped region (native res), "
            "the second is the full viewport for context. The elements list is only "
            "what's inside the box."
        )

    vitals = md.get("vitals") or {}
    parts = []
    if vitals.get("lcp_ms") is not None:
        parts.append(f"LCP {vitals['lcp_ms']}ms")
    if vitals.get("fcp_ms") is not None:
        parts.append(f"FCP {vitals['fcp_ms']}ms")
    if vitals.get("cls") is not None:
        parts.append(f"CLS {vitals['cls']}")
    if vitals.get("long_tasks"):
        parts.append(f"{vitals['long_tasks']} long task(s)")
    if parts:
        lines.append(f"- vitals: {', '.join(parts)}")

    meta = md.get("meta") or {}
    meta_bits = [f"{k}={meta[k]!r}" for k in ("lang", "description", "canonical") if meta.get(k)]
    missing = [k for k in ("description", "og_title") if k in meta and not meta.get(k)]
    if meta_bits or missing:
        if missing:
            meta_bits.append(f"MISSING: {', '.join(missing)}")
        lines.append("- head: " + "; ".join(meta_bits))

    # Findings first — these are the punch-list seeds.
    audit = md.get("audit") or {}
    for key, heading in (
        ("contrast", "Contrast violations (worst first)"),
        ("a11y", "Accessibility issues"),
        ("images", "Image issues"),
    ):
        items = audit.get(key) or []
        if items:
            lines.append(f"\n## {heading} ({len(items)})")
            lines += [f"- {i}" for i in items[:12]]

    console = md.get("console") or md.get("issues") or []
    if console:
        lines.append(f"\n## Console / network issues ({len(console)})")
        lines += [f"- {c}" for c in console[:20]]

    third = md.get("third_parties") or []
    if third:
        lines.append(f"\n## Third-party origins ({len(third)}, heaviest first)")
        for t in third[:8]:
            kb = f", {t['kb']}kB" if t.get("kb") else ""
            lines.append(f"- {t['host']}: {t['requests']} req{kb}, slowest {t['slowest_ms']}ms")

    legal = cap.get("linked") or md.get("legal_links") or []
    if legal:
        lines.append(f"\n## Linked policy pages ({len(legal)})")
        for l in legal[:8]:
            if l.get("file"):
                extra = f" — saved as {l['file']} in the capture folder (Read it for the full document)"
            elif l.get("ok") is False:
                extra = " — fetch FAILED at capture time"
            else:
                extra = ""
            lines.append(f"- {l.get('kind')}: {l.get('url')}{extra}")

    elements = md.get("elements") or []
    if elements:
        lines.append(f"\n## Visible interactive elements ({len(elements)})")
        for e in elements[:40]:
            extras = []
            if e.get("tile") is not None:
                extras.append(f"tile {e['tile']}")
            if e.get("contrast") is not None and e["contrast"] < 4.5:
                extras.append(f"contrast {e['contrast']}:1")
            suffix = f" [{', '.join(extras)}]" if extras else ""
            lines.append(
                f"- `{e.get('selector')}` {e.get('rect')} — {str(e.get('text', ''))[:60]}{suffix}"
            )

    lines.append(
        f"\n## {len(cap.get('tiles', []))} screenshot tile(s) below, top → bottom."
    )
    return "\n".join(lines)


@mcp.tool()
def get_latest_capture() -> list:
    """Pull the most recently clipped page into the session: a text summary
    (url, your note, console errors, visible interactive elements with their
    CSS selectors) followed by the scroll-tile screenshots as images."""
    cap = _STORE["latest"]
    if not cap:
        return ["No capture yet — clip a page from the browser extension first."]
    out: list[Any] = [_summary(cap)]
    for b64 in cap.get("tiles", []):
        out.append(Image(data=base64.b64decode(b64), format="png"))
    return out


@mcp.tool()
def list_recent_captures() -> str:
    """List recent capture metadata (url, title, note, time) without images."""
    h = _STORE["history"]
    return json.dumps(h, indent=2) if h else "No captures yet."


def _apply_cli(argv: list[str]) -> None:
    """Tiny arg handling — a CLI flag works identically on systemd/launchd/
    Task Scheduler, unlike env vars (Windows tasks can't set them cleanly).

      --capture-dir PATH   where clip folders are written (default: ./captures)
      --no-save            keep captures in memory only
    """
    global _SAVE, _SAVE_ROOT
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--capture-dir" and args:
            _SAVE_ROOT = pathlib.Path(args.pop(0)).expanduser()
        elif a.startswith("--capture-dir="):
            _SAVE_ROOT = pathlib.Path(a.split("=", 1)[1]).expanduser()
        elif a == "--no-save":
            _SAVE = False


def main() -> None:
    """Console-script entry point (clip-bridge)."""
    _apply_cli(sys.argv[1:])
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
