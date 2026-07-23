#!/usr/bin/env python3
"""
Agent status collector — one per machine, binds 127.0.0.1 only.

Claude Code hooks POST session-state updates here (see agent-status-emit.py).
The cockpit (laptop) reads GET /status, either directly (local machine) or
through an SSH tunnel (remote machines). Nothing is ever exposed publicly.

State is in-memory: session_id -> latest status. Sessions age out on SessionEnd
or after a long silence. Restarting the service simply starts empty; the next
hook from any live session repopulates it within one tool call.
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("AGENT_COLLECTOR_PORT", "8458"))
# Label for this machine in the cockpit. Override on generic VPS hostnames.
MACHINE = os.environ.get("AGENT_COCKPIT_MACHINE") or socket.gethostname()

# A "working" session that hasn't sent a heartbeat in this many seconds is
# reclassified as "stale" (likely crashed / SSH dropped mid-run). Idle sessions
# legitimately stay quiet, so staleness only applies to active states.
STALE_AFTER = float(os.environ.get("AGENT_STALE_AFTER", "45"))
ACTIVE_STATES = {"working", "starting"}

_lock = threading.Lock()
_sessions = {}  # session_id -> dict


def _now():
    return time.time()


def ingest(payload):
    sid = payload.get("session_id")
    if not sid:
        return
    state = payload.get("state", "unknown")
    with _lock:
        if state == "ended":
            _sessions.pop(sid, None)
            return
        prev = _sessions.get(sid, {})
        _sessions[sid] = {
            "session_id": sid,
            "machine": MACHINE,
            "project": payload.get("project") or prev.get("project") or "?",
            "cwd": payload.get("cwd") or prev.get("cwd") or "",
            "state": state,
            "event": payload.get("event", ""),
            "detail": payload.get("detail", ""),
            # First prompt wins — a stable name for the conversation.
            "title": prev.get("title") or payload.get("title", ""),
            # Latest prompt wins — the thing you most recently asked for.
            "last_prompt": payload.get("last_prompt") or prev.get("last_prompt", ""),
            # Latest "what Claude just said" snippet (changes as work progresses).
            "activity": payload.get("activity") or prev.get("activity", ""),
            # Context size + model, derived from the transcript's usage block.
            "context_tokens": payload.get("context_tokens", prev.get("context_tokens", 0)),
            "model": payload.get("model") or prev.get("model", ""),
            # Live subagent count (best-effort).
            "subagents": payload.get("subagents", prev.get("subagents", 0)),
            # Explicit window identity, if the emitter had it.
            "window_name": payload.get("window_name") or prev.get("window_name", ""),
            "window_color": payload.get("window_color") or prev.get("window_color", ""),
            "first_seen": prev.get("first_seen", _now()),
            "last_seen": _now(),
        }


def snapshot():
    now = _now()
    out = []
    with _lock:
        for s in _sessions.values():
            c = dict(s)
            age = now - c["last_seen"]
            c["age"] = age
            if c["state"] in ACTIVE_STATES and age > STALE_AFTER:
                c["state"] = "stale"
            out.append(c)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            self._json({"machine": MACHINE, "sessions": snapshot()})
        elif self.path == "/health":
            self._json({"ok": True, "machine": MACHINE})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/ingest":
            self._json({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode() or "{}")
            ingest(payload)
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 400)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"agent-collector [{MACHINE}] on http://{HOST}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
