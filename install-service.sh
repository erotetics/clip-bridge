#!/usr/bin/env bash
# One-command setup: install the Clip agent bridge as an always-on user service.
#   Linux → systemd user service       macOS → launchd user agent
#
#   ./install-service.sh                          install + start
#   ./install-service.sh --capture-dir ~/clips    …saving clips to a chosen folder
#   ./install-service.sh uninstall                stop + remove
#
# Windows: use install-service.ps1 instead.
# The bridge starts on login and restarts if it dies. Logs:
#   Linux:  journalctl --user -u clip-bridge -f
#   macOS:  tail -f ~/Library/Logs/clip-bridge.log

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$BRIDGE_DIR/.venv/bin/clip-bridge"
OS="$(uname -s)"

ACTION="install"
CAPTURE_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    uninstall) ACTION="uninstall" ;;
    --capture-dir) CAPTURE_DIR="$2"; shift ;;
    --capture-dir=*) CAPTURE_DIR="${1#*=}" ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

ensure_venv() {
  [ -x "$BIN" ] && return
  command -v python3 >/dev/null || {
    echo "python3 not found — install it first (macOS: xcode-select --install)"; exit 1; }
  echo "Setting up venv (first run)…"
  python3 -m venv "$BRIDGE_DIR/.venv"
  "$BRIDGE_DIR/.venv/bin/pip" install -q -e "$BRIDGE_DIR"
}

health_check() {
  sleep 2
  if curl -sf -m 3 http://127.0.0.1:7777/health >/dev/null; then
    echo "✔ clip-bridge running on http://localhost:7777 (MCP at /mcp)"
    echo "  starts on login, auto-restarts on failure"
    return 0
  fi
  echo "✘ service installed but health check failed — see logs:"
  return 1
}

case "$OS" in

Linux)
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  UNIT="$UNIT_DIR/clip-bridge.service"
  if [ "$ACTION" = "uninstall" ]; then
    systemctl --user disable --now clip-bridge.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload
    echo "clip-bridge service removed."; exit 0
  fi
  command -v systemctl >/dev/null || {
    echo "No systemd here — run the bridge directly instead: $BIN"; exit 1; }
  ensure_venv
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Clip agent bridge (MCP) on localhost:7777

[Service]
ExecStart=$BIN${CAPTURE_DIR:+ --capture-dir "$CAPTURE_DIR"}
Restart=on-failure
RestartSec=2
# Optional env overrides (systemctl --user edit clip-bridge):
#   Environment=CLIP_NO_SAVE=1
#   Environment=CLIP_AUTO_REVIEW=1

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable clip-bridge.service
  # restart (not just start): a re-install must replace a running instance so
  # the new ExecStart takes effect.
  systemctl --user restart clip-bridge.service
  health_check || { echo "  journalctl --user -u clip-bridge -n 50"; exit 1; }
  echo "  logs:   journalctl --user -u clip-bridge -f"
  echo "  status: systemctl --user status clip-bridge"
  ;;

Darwin)
  LABEL="com.clip.bridge"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  LOG="$HOME/Library/Logs/clip-bridge.log"
  GUI="gui/$(id -u)"
  if [ "$ACTION" = "uninstall" ]; then
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "clip-bridge agent removed."; exit 0
  fi
  ensure_venv
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>${CAPTURE_DIR:+
    <string>--capture-dir</string>
    <string>$CAPTURE_DIR</string>}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  # Re-bootstrap cleanly if a previous version is loaded.
  launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$GUI" "$PLIST"
  health_check || { echo "  tail -50 $LOG"; exit 1; }
  echo "  logs:   tail -f $LOG"
  echo "  status: launchctl print $GUI/$LABEL | head -20"
  ;;

*)
  echo "Unsupported OS: $OS — run the bridge manually: $BIN"
  exit 1
  ;;
esac
