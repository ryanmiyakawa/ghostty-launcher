#!/usr/bin/env python3
"""
Agent status emitter — Claude Code hook, identical on every machine.

Reads the hook JSON on stdin, maps the event to a coarse session state, enriches
it with lightweight signals (what Claude last said, how many subagents are live,
the pending permission), and fire-and-forgets it to the local collector
(127.0.0.1:8458). It ALWAYS exits 0 and never blocks Claude: the POST has a hard
0.5s timeout and every step is defensive.

Wire to: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification,
Stop, SubagentStop, SessionEnd (see the settings.json snippet / install script).
"""

import json
import os
import re
import sys
import urllib.request

PORT = os.environ.get("AGENT_COLLECTOR_PORT", "8458")
URL = f"http://127.0.0.1:{PORT}/ingest"

# hook_event_name -> session state
EVENT_STATE = {
    "SessionStart": "starting",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Notification": "needs_input",  # blocked: permission or idle-wait
    "Stop": "done",                 # finished its turn — your move
    "SubagentStop": "working",      # a subagent finished; main agent continues
    "SessionEnd": "ended",
}

SUBAGENT_TOOLS = {"Task", "Agent"}


def project_name(cwd):
    if not cwd:
        return "?"
    return os.path.basename(cwd.rstrip("/")) or cwd


def _sub_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:64]
    return f"/tmp/agent-sub-{safe}.cnt" if safe else None


def subagent_count(sid, delta=0, reset=False):
    """Track live subagents with a tiny per-session counter file. Approximate,
    but spawn/finish for one agent are serialized so it stays sane."""
    p = _sub_path(sid)
    if not p:
        return 0
    if reset:
        try:
            os.remove(p)
        except OSError:
            pass
        return 0
    n = 0
    try:
        with open(p) as f:
            n = int(f.read() or 0)
    except (IOError, ValueError):
        n = 0
    if delta:
        n = max(0, n + delta)
        try:
            with open(p, "w") as f:
                f.write(str(n))
        except IOError:
            pass
    return n


def read_transcript(path):
    """Tail the transcript (cheap — only the final chunk) and pull orientation
    signals: what Claude last said, the context size + model from the latest
    `usage` block. The hook payload has none of this, but the transcript does."""
    info = {"activity": "", "context_tokens": 0, "model": ""}
    if not path or not os.path.exists(path):
        return info
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 131072))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return info
    for line in tail.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", {})
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                info["activity"] = block["text"]
        u = msg.get("usage")
        if u:
            info["context_tokens"] = (u.get("input_tokens", 0)
                                      + u.get("cache_creation_input_tokens", 0)
                                      + u.get("cache_read_input_tokens", 0))
        if msg.get("model"):
            info["model"] = msg["model"]
    info["activity"] = " ".join(info["activity"].split())[:200]
    return info


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return

    event = hook.get("hook_event_name", "")
    state = EVENT_STATE.get(event)
    if state is None:
        return

    sid = hook.get("session_id", "")
    cwd = hook.get("cwd") or os.getcwd()

    # Maintain the live subagent count.
    if event == "PreToolUse" and hook.get("tool_name") in SUBAGENT_TOOLS:
        subs = subagent_count(sid, +1)
    elif event == "SubagentStop":
        subs = subagent_count(sid, -1)
    elif event == "SessionEnd":
        subagent_count(sid, reset=True)
        subs = 0
    else:
        subs = subagent_count(sid)

    payload = {
        "session_id": sid,
        "event": event,
        "state": state,
        "cwd": cwd,
        "project": project_name(cwd),
        "subagents": subs,
    }

    if event == "Notification":
        payload["detail"] = (hook.get("message") or "")[:120]
    elif event in ("PreToolUse", "PostToolUse"):
        payload["detail"] = hook.get("tool_name", "")
    elif event == "UserPromptSubmit":
        payload["title"] = " ".join((hook.get("prompt") or "").split())[:90]

    # What Claude last said + context size/model — richest orientation signals.
    # Skip on the highest frequency event (PreToolUse) to keep tool calls snappy.
    if event in ("PostToolUse", "Stop", "Notification", "SubagentStop"):
        info = read_transcript(hook.get("transcript_path"))
        if info["activity"]:
            payload["activity"] = info["activity"]
        if info["context_tokens"]:
            payload["context_tokens"] = info["context_tokens"]
        if info["model"]:
            payload["model"] = info["model"]

    if os.environ.get("AGENT_WINDOW_NAME"):
        payload["window_name"] = os.environ["AGENT_WINDOW_NAME"]
    if os.environ.get("AGENT_WINDOW_COLOR"):
        payload["window_color"] = os.environ["AGENT_WINDOW_COLOR"]

    try:
        req = urllib.request.Request(
            URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=0.5).read()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
