"""Clip → Claude Code bridge.

One local process, two faces:
  - HTTP POST /capture     ← the browser extension sends a clipped page here
  - MCP (streamable-http)  ← Claude Code connects and pulls the capture in

The capture (scroll-tile screenshots + page metadata + your annotation) is held
in memory; Claude Code calls `get_latest_capture` to bring it into the session.

Two ways to run it:
  stdio (recommended — the MCP client launches it, nothing to install as a service):
      claude mcp add capture -- uvx clip-bridge --stdio
    While the session runs it also listens on 127.0.0.1:7777 for the extension.
    If another bridge already holds the port (a second session, or the
    always-on service), this one reads clips from it instead.
  always-on service (HTTP; see install-service.sh):
      clip-bridge             (listens on 127.0.0.1:7777, MCP at /mcp)
      claude mcp add --transport http capture http://localhost:7777/mcp
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import re
import shutil
import socket
import sys
import time
import urllib.request
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from starlette.requests import Request
from starlette.responses import JSONResponse

HOST, PORT = "127.0.0.1", 7777
mcp = FastMCP("capture", host=HOST, port=PORT)

# Where clips/reviews are written and where headless agents run. A source
# checkout keeps everything beside the code (as before); a pip/uvx install
# lives in a throwaway env, so it uses ~/.clip-bridge instead. CLIP_HOME overrides.
_HERE = pathlib.Path(__file__).resolve().parent
_FROM_SOURCE = (_HERE / "pyproject.toml").exists()
_HOME = pathlib.Path(
    os.environ.get("CLIP_HOME") or (_HERE if _FROM_SOURCE else pathlib.Path.home() / ".clip-bridge")
).expanduser()

# Opt-in "seamless" mode: when a clip arrives, fire off a Claude Code agent that
# pulls it and reviews it — no human prompt. Set CLIP_AUTO_REVIEW=1 to enable.
# (MCP itself is pull-only; it can't push a turn into a live session. This makes
# the *clip* the trigger instead.)
AUTO_REVIEW = os.environ.get("CLIP_AUTO_REVIEW") == "1"
_REVIEWS = _HOME / "reviews"
_REPO_ROOT = _HERE.parent if _FROM_SOURCE else pathlib.Path.home()


# Auto-answer: when a clip arrives WITH a note, spawn a headless agent that
# pulls the capture and answers the note; the extension polls /answer and
# paints the reply onto the page. Opt-in (CLIP_AUTO_ANSWER=1): it spends the
# user's Claude usage, so it must never run without them asking for it.
AUTO_ANSWER = os.environ.get("CLIP_AUTO_ANSWER") == "1"


async def _auto_answer(answer_id: str, note: str) -> None:
    claude = shutil.which("claude")
    store = _STORE["answers"]
    if not claude:
        store[answer_id] = {"status": "error", "text": "claude CLI not found"}
        return
    cfg = '{"mcpServers":{"capture":{"type":"http","url":"http://127.0.0.1:7777/mcp"}}}'
    try:
        proc = await asyncio.create_subprocess_exec(
            claude, "-p",
            "A web page was just clipped in the browser with this question attached: "
            f"\"{note}\". Call get_latest_capture, then answer the question directly "
            "and concisely in plain markdown — no preamble, no meta-commentary about "
            "the capture itself. If the capture can't answer it, say what's missing.",
            "--max-turns", "10",
            "--mcp-config", cfg,
            "--allowedTools", "mcp__capture__get_latest_capture",
            cwd=str(_REPO_ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=150)
        text = (out or b"").decode(errors="replace").strip()
        store[answer_id] = {"status": "done", "text": text or "(empty answer)"}
    except asyncio.TimeoutError:
        store[answer_id] = {"status": "error", "text": "answer timed out"}
    except Exception as exc:  # noqa: BLE001
        store[answer_id] = {"status": "error", "text": f"answer failed: {exc}"}
    # keep the store tiny
    for k in list(store)[:-10]:
        store.pop(k, None)


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
_STORE: dict[str, Any] = {"latest": None, "history": [], "delivered": True, "answers": {}}

# Persist each clip to disk ("files on the computer"): tiles as PNGs + clip.json.
# On by default to bridge/captures/; CLIP_CAPTURE_DIR overrides, CLIP_NO_SAVE=1 off.
_SAVE = os.environ.get("CLIP_NO_SAVE") != "1"
_SAVE_ROOT = pathlib.Path(os.environ.get("CLIP_CAPTURE_DIR") or _HOME / "captures")

# Does this process own the :7777 listener (and so the clip store)? Always true
# in service mode; in stdio mode a second bridge reads from the owner instead.
_OWNER = True


def _peer(path: str) -> Any:
    """GET JSON from the bridge that owns the listener (stdio, port taken)."""
    with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}", timeout=15) as r:
        return json.load(r)


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
    answer_id = None
    if AUTO_ANSWER and (data.get("note") or "").strip():
        answer_id = str(data["received_at"])
        _STORE["answers"][answer_id] = {"status": "pending", "text": ""}
        asyncio.create_task(_auto_answer(answer_id, data["note"].strip()))
    return JSONResponse(
        {
            "ok": True,
            "tiles": len(data.get("tiles", [])),
            "auto_review": AUTO_REVIEW,
            "answer_id": answer_id,
        }
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


@mcp.custom_route("/latest", methods=["GET"])
async def latest(request: Request) -> JSONResponse:
    """Full latest clip (tiles included) — how a peer stdio bridge reads it."""
    return JSONResponse({"latest": _STORE["latest"], "history": _STORE["history"]})


@mcp.custom_route("/answer/{answer_id}", methods=["GET"])
async def answer(request: Request) -> JSONResponse:
    """The extension polls this to paint the auto-answer onto the page."""
    entry = _STORE["answers"].get(request.path_params["answer_id"])
    if not entry:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse(entry)


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
    cap = _store()["latest"]
    if not cap:
        return ["No capture yet — clip a page from the browser extension first."]
    out: list[Any] = [_summary(cap)]
    for b64 in cap.get("tiles", []):
        out.append(Image(data=base64.b64decode(b64), format="png"))
    return out


@mcp.tool()
def list_recent_captures() -> str:
    """List recent capture metadata (url, title, note, time) without images."""
    h = _store()["history"]
    return json.dumps(h, indent=2) if h else "No captures yet."


def _store() -> dict:
    """The clip store: ours if we own the listener, else the owner's."""
    if _OWNER:
        return _STORE
    try:
        return _peer("/latest")
    except Exception:  # owner just went away; our listener loop takes over soon
        return {"latest": None, "history": []}


def _apply_cli(argv) -> None:
    """Tiny arg handling — a CLI flag works identically on systemd/launchd/
    Task Scheduler, unlike env vars (Windows tasks can't set them cleanly).

      --stdio              MCP over stdio, launched by the client (see module doc)
      --capture-dir PATH   where clip folders are written (default: <home>/captures)
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


def _try_bind() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":  # on Windows SO_REUSEADDR would allow a double bind
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError:
        sock.close()
        return None
    return sock


async def _hold_listener(holder: dict) -> None:
    """stdio mode: serve the extension's HTTP side on :7777 for as long as the
    session lives. If the port is taken, stay a peer and retry — so when the
    owning bridge exits, the next live one picks up the clips."""
    global _OWNER
    while (sock := _try_bind()) is None:
        await asyncio.sleep(3)
    _OWNER = True
    # Logging stays off stdout: stdout is the MCP stdio channel.
    config = uvicorn.Config(
        mcp.streamable_http_app(), log_config=None, log_level="warning", access_log=False
    )
    server = holder["server"] = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # the stdio session owns shutdown
    await server.serve(sockets=[sock])


async def _run_stdio() -> None:
    global _OWNER
    _OWNER = False
    holder: dict = {}
    listener = asyncio.create_task(_hold_listener(holder))
    try:
        await mcp.run_stdio_async()
    finally:
        if "server" in holder:  # graceful: let uvicorn run its lifespan shutdown
            holder["server"].should_exit = True
            await asyncio.wait({listener}, timeout=5)
        listener.cancel()


def main() -> None:
    """Console-script entry point (clip-bridge)."""
    if "--stdio" in sys.argv[1:]:
        _apply_cli(a for a in sys.argv[1:] if a != "--stdio")
        asyncio.run(_run_stdio())
    else:
        _apply_cli(sys.argv[1:])
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
