#!/usr/bin/env bash
# Install/refresh the Cockpit on this Mac: symlink the repo scripts into
# ~/.claude and load the collector launchd service. The dashboard itself is
# already managed by com.rhmiyakawa.ghostty-launcher (see restart script).
# Idempotent — safe to re-run after pulling.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.claude"

# Standard paths -> repo (keeps settings.json hooks + plists stable).
ln -sf "$REPO/collector.py"         "$HOME/.claude/agent-collector.py"
ln -sf "$REPO/emit.py"              "$HOME/.claude/agent-status-emit.py"
ln -sf "$REPO/ghostty_dashboard.py" "$HOME/.claude/ghostty_dashboard.py"

# Collector launchd service.
cp "$REPO/deploy/com.rhm.agent-collector.plist" "$HOME/Library/LaunchAgents/"
launchctl bootout "gui/$(id -u)/com.rhm.agent-collector" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.rhm.agent-collector.plist"

echo "✓ Collector loaded; dashboard on http://127.0.0.1:8457 (Cockpit tab)."
echo "  Ensure deploy/hooks.json's 'hooks' block is present in ~/.claude/settings.json."
