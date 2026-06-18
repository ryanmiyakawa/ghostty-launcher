#!/usr/bin/env bash
# Restart the Ghostty launcher dashboard service (local web UI on port 8457,
# served by ~/.claude/ghostty_dashboard.py). Stops any existing listener, then
# starts a fresh detached instance (no browser pop) logging to a file.
set -u

PORT=8457
LAUNCHER="$HOME/.claude/ghostty-launcher"
LOG="$HOME/.claude/ghostty_dashboard.log"
URL="http://127.0.0.1:$PORT"

listeners() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null; }

# 1. Stop any existing service on the port.
pids="$(listeners)"
if [ -n "$pids" ]; then
  echo "Stopping existing service (PID: $(echo "$pids" | tr '\n' ' '))…"
  kill $pids 2>/dev/null
  sleep 1
  pids="$(listeners)"
  if [ -n "$pids" ]; then
    echo "Force-killing…"
    kill -9 $pids 2>/dev/null
    sleep 1
  fi
else
  echo "No existing service on port $PORT."
fi

# 2. Start a fresh, detached instance (no browser).
echo "Starting Ghostty launcher…"
nohup "$LAUNCHER" --no-browser >"$LOG" 2>&1 &
disown 2>/dev/null || true

# 3. Verify it came up.
for _ in 1 2 3 4 5; do
  sleep 0.5
  [ -n "$(listeners)" ] && break
done

if [ -n "$(listeners)" ]; then
  echo "✓ Ghostty launcher running at $URL (PID: $(listeners | tr '\n' ' '), log: $LOG)"
else
  echo "✗ Failed to start — last log lines:"
  tail -n 8 "$LOG" 2>/dev/null
  exit 1
fi
