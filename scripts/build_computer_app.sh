#!/usr/bin/env bash
# Build the signed .app the computer-use MCP server has to run inside.
#
# Four things make macOS willing to let a process change which application is
# frontmost, and three of them are decided here rather than in the code
# (ADR-092 §2): a bundle identity, a code signature, and -- via LSUIElement --
# an activation policy. The fourth, a live main-thread run loop, is in
# `adapters/screen/darwin.py`. Measured 2026-08-29: with all four, activation
# took 15/15; with any one missing, 0/N.
#
# Three properties of this script are decisions rather than mechanics:
#
#   * **It builds outside the checkout.** LaunchServices will not launch a
#     bundle from a path containing a hidden directory, which every git
#     worktree here lives under (`.claude/worktrees/...`), and this repository's
#     own path contains non-ASCII characters besides. Measured: `open -a` on a
#     bundle under the worktree produced no process and no error at all.
#   * **It signs, ad-hoc.** An unsigned bundle can be added to the
#     Accessibility list and toggled on, and `AXIsProcessTrusted()` still
#     answers false -- observed on 2026-08-29 before this script existed. The
#     grant attaches to a code identity, so there has to be one.
#   * **The bundle id is stable.** TCC keys the grant on it together with the
#     signature, so changing either costs the person another trip to System
#     Settings. Rebuilding with this script does not.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_ID="com.agent-workbench.computer-mcp"
APP_NAME="AgentComputerMCP"
DEST="${AW_COMPUTER_APP_DIR:-$HOME/Applications}/${APP_NAME}.app"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "this bundle is macOS-only; nothing to build on $(uname -s)" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "no interpreter at $PYTHON -- run 'uv sync --extra computer-use' first" >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS"

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>Agent Workbench Computer MCP</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <!-- Accessory: registered with the window server, which is what activation
       needs, while owning no Dock icon and no menu bar. A server with an icon
       in the Dock would be announcing itself as an application to switch to. -->
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$DEST/Contents/MacOS/launcher" <<LAUNCH
#!/bin/bash
# Absolute paths: LaunchServices gives this no useful working directory and
# almost none of the environment a shell would.
#
# The log is not a debugging convenience. Started from LaunchServices this
# process has nowhere to write -- no terminal, no journal -- so a server that
# exits at startup (a missing grant, a port already bound) would vanish
# leaving an operator nothing at all to read. That is the failure mode
# ADR-070 §4 spent a paragraph refusing.
LOG="\$HOME/Library/Logs/${APP_NAME}.log"
mkdir -p "\$(dirname "\$LOG")"
exec "${PYTHON}" -m agent_workbench.apps.computer_mcp.main "\$@" >>"\$LOG" 2>&1
LAUNCH
chmod +x "$DEST/Contents/MacOS/launcher"

/usr/bin/plutil -lint "$DEST/Contents/Info.plist" >/dev/null
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
codesign --force --deep --sign - "$DEST"
codesign --verify --verbose "$DEST" 2>&1 | sed 's/^/  /'

echo "built: $DEST"
echo
echo "Grant it both permissions once, then start it:"
echo "  System Settings > Privacy & Security > Accessibility   -> add ${APP_NAME}"
echo "  System Settings > Privacy & Security > Screen Recording -> add ${APP_NAME}"
echo "  open -a \"$DEST\""
echo
echo "macOS reads both grants at launch, so restart the app after granting."
echo "Log: ~/Library/Logs/${APP_NAME}.log"
