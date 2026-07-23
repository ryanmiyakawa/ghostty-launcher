#!/usr/bin/env bash
# Install the Cockpit collector on a remote machine (VPS). Run this ON the VPS,
# from inside a clone of this repo. It sets up the localhost-only collector as a
# user systemd service and points ~/.claude at the repo's scripts. Nothing binds
# a public port — the Mac reaches this collector only through an SSH tunnel.
#
# Usage:  ./install-remote.sh <machine-label>
#   e.g.  ./install-remote.sh vps1
set -euo pipefail

LABEL="${1:-$(hostname -s)}"
REPO="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.claude"

# 1. Point the standard hook/collector paths at the repo (idempotent symlinks).
ln -sf "$REPO/collector.py" "$HOME/.claude/agent-collector.py"
ln -sf "$REPO/emit.py"      "$HOME/.claude/agent-status-emit.py"

# 2. Install the user systemd service.
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s/CHANGE_ME/$LABEL/" "$REPO/deploy/agent-collector.service.template" \
    > "$UNIT_DIR/agent-collector.service"
systemctl --user daemon-reload
systemctl --user enable --now agent-collector
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "(!) Could not enable-linger; collector stops when you log out. Run: sudo loginctl enable-linger $USER"

echo
echo "✓ Collector installed as machine '$LABEL' (systemctl --user status agent-collector)"
echo
echo "NEXT — two manual steps:"
echo "  1. Merge deploy/hooks.json's 'hooks' block into ~/.claude/settings.json here."
echo "  2. On the Mac, add this host to ~/.claude/agent-cockpit-hosts.json:"
echo "       {\"name\": \"$LABEL\", \"ssh\": \"$USER@$(hostname)\", \"local_port\": <pick a free port e.g. 9001>}"
echo "     then restart the dashboard (restart-ghostty-launcher.sh)."
