# Clip agent bridge (MCP)

> Companion to the **Clip** Chrome extension by [Erotetic Arts](https://erotetics.com):
> clip any web page (screenshots + selectors + a11y/vitals audit + the legal
> documents it links to) and pull it into Claude Code, Cursor, or any MCP
> client. Extension install link available on request while the store listing
> is unlisted — hello@erotetics.com.

A tiny local process that pipes a clipped browser page (scroll-tile screenshots
+ page metadata + your annotation) into your AI coding agent over MCP —
Claude Code, Cursor, Windsurf, Zed, or any other MCP client.

**One process, two faces** (verified design):
- `POST /capture` — the browser extension sends a clipped page here.
- MCP over streamable-HTTP at `/mcp` — your agent connects and pulls it in.

Nothing leaves the machine: the capture is held in memory (and optionally on
disk, under your control). This is the local companion of the **Clip** browser
extension — one capture engine with pluggable destinations; this bridge is the
"send it to my AI agent" destination. No backend, no accounts, no egress.

## Setup (recommended): one command, always running

The installers create the venv if needed and register the bridge as an
always-on user service: it starts on login, restarts if it dies, and is simply
there — so MCP clients always find it at session start.

```bash
# Linux (systemd) and macOS (launchd)
./install-service.sh
./install-service.sh --capture-dir ~/clips   # choose where clip files are saved
```

```powershell
# Windows (Task Scheduler, hidden window, no admin needed)
powershell -ExecutionPolicy Bypass -File .\install-service.ps1
powershell -ExecutionPolicy Bypass -File .\install-service.ps1 -CaptureDir "$HOME\clips"
```

Re-running the installer with different options replaces the running service —
that's also how you *change* the capture folder later.

| | status | logs | remove |
|---|---|---|---|
| Linux | `systemctl --user status clip-bridge` | `journalctl --user -u clip-bridge -f` | `./install-service.sh uninstall` |
| macOS | `launchctl print gui/$(id -u)/com.clip.bridge` | `tail -f ~/Library/Logs/clip-bridge.log` | `./install-service.sh uninstall` |
| Windows | `Get-ScheduledTaskInfo -TaskName ClipBridge` | (run `python -m server` in a console to debug) | `install-service.ps1 -Uninstall` |

- Env overrides (`CLIP_CAPTURE_DIR`, `CLIP_NO_SAVE`, `CLIP_AUTO_REVIEW`):
  Linux `systemctl --user edit clip-bridge`; macOS add an `EnvironmentVariables`
  dict to the plist; Windows edit the task's action.
- Linux, survive logout (optional): `loginctl enable-linger $USER`
- The Linux path is battle-tested; **macOS and Windows installers are fresh** —
  if one misbehaves, the manual run below always works, and issue reports are welcome.

## Run manually (any platform)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .          # or: pip install -r requirements.txt
clip-bridge               # console script (= python server.py)
# listens on 127.0.0.1:7777, MCP at /mcp
```

## Captures saved to disk

Each clip is written to `captures/<ts>-<slug>/` as `tile00.png…` + `clip.json`,
so you keep local copies and your agent can read full-res tiles by path (MCP may
downscale). Choose the folder with `--capture-dir PATH` (installer flag or when
running `clip-bridge` directly); disable saving with `--no-save`. The env vars
`CLIP_CAPTURE_DIR` / `CLIP_NO_SAVE` still work as fallbacks.

## Tests

```bash
pip install -e ".[dev]" && pytest -q   # hermetic: no network, no real Claude
```

## Wire into your agent (any MCP client)

The bridge speaks standard streamable-HTTP MCP at `http://localhost:7777/mcp` —
register it however your client registers MCP servers:

```bash
# Claude Code
claude mcp add --transport http capture http://localhost:7777/mcp
```

```json
// Cursor (.cursor/mcp.json) / Windsurf / most JSON-config clients
{ "mcpServers": { "capture": { "url": "http://localhost:7777/mcp" } } }
```

Or commit a project-scoped `.mcp.json`:

```json
{ "mcpServers": { "capture": { "type": "http", "url": "http://localhost:7777/mcp" } } }
```

**In the extension:** open **Options → Capture mode → AI agent (MCP)** (bridge URL
defaults to `http://localhost:7777`). Without this, the capture button still
targets the backend, not this bridge.

Then in a session: clip a page (or run `python make_capture.py` to inject a
sample), and ask *"pull in the page I just clipped."* The agent calls
`get_latest_capture` and receives the metadata + screenshots.

The seamless extras below (session hook, auto-review) are **Claude Code-specific**;
other clients use the plain pull model above.

## Tools exposed

- `get_latest_capture` — text summary (url, your note, console errors, visible
  interactive elements + CSS selectors) followed by the scroll-tile screenshots
  as images.
- `list_recent_captures` — recent capture metadata, no images.

## Verify without the extension

```bash
python server.py &          # start it
python make_capture.py      # simulate the extension's POST (sample page)
python test_client.py       # MCP client: prints exactly what Claude Code receives
```

## Seamless modes ("ping Claude on clip")

MCP is pull-only — it can't push a turn into a live session. Two ways to make a
clip feel like it pings Claude anyway:

**A. Auto-attach into your open session (hook + punch-list)** — recommended.
`hooks/clip_context.py` is a `UserPromptSubmit` hook: on your next prompt it polls
`/pending`, and if a new clip arrived it injects the clip's metadata as context —
so the session sees it without you mentioning it — and asks the agent to append a
dated entry to `PUNCHLIST.md`. Fires once per clip (the bridge marks it delivered),
no-ops if the bridge is down. Wired in `.claude/settings.local.json`:

```json
{ "hooks": { "UserPromptSubmit": [ { "hooks": [
  { "type": "command", "command": "python3 .../bridge/hooks/clip_context.py" }
] } ] } }
```

You clip while browsing, then just keep talking to Claude — the clip is already
there, and your punch-list grows. Remove the hooks block (or stop the bridge) to
turn it off.

**B. Auto-trigger a headless review** — `CLIP_AUTO_REVIEW=1 python server.py`.
On each clip the bridge spawns `claude -p` (haiku) that pulls the capture and
writes a punch-list to `reviews/`. Fully hands-free, but it's a *separate* agent
run, not your interactive session. Good for "clip and forget"; repoint the output
to a desktop notification / PR comment as desired.

## Notes / next

- **HTTP transport, not stdio** — the bridge must outlive any one session and
  serve two clients (extension + Claude Code), so it's a standalone listener.
- Scroll tiles are large; mind Claude Code's `MAX_MCP_OUTPUT_TOKENS`. Return
  tiles on request and downscale in the extension before sending.
- Images from MCP tool results may not render *inline* in the visible reply
  (Claude Code issue #1329) but Claude still processes them — confirmed working.
