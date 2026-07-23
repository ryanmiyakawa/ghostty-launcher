#!/usr/bin/env python3
"""
Ghostty Terminal Launcher Dashboard - Web UI
A browser-based launcher with configuration and color picker.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.path.expanduser("~/.claude/ghostty_dashboard_config.json")
PORT = 8457

STATUS_REPO = os.path.expanduser("~/project-status")
STATUS_JSON = os.path.join(STATUS_REPO, "status.json")

# ---- Cockpit: live agent status aggregation ----------------------------------
# The Cockpit tab shows every running Claude Code session across all machines.
# Each machine runs a localhost-only collector (collector.py, :8458). This
# dashboard reads the local one directly and every remote one through an SSH
# tunnel it maintains itself (hosts from agent-cockpit-hosts.json). See README.
LOCAL_COLLECTOR = int(os.environ.get("AGENT_COLLECTOR_PORT", "8458"))
HOSTS_PATH = os.path.expanduser("~/.claude/agent-cockpit-hosts.json")
# User-editable per-session labels + manual card order, persisted on the Mac.
COCKPIT_UI_PATH = os.path.expanduser("~/.claude/agent-cockpit-ui.json")

_tunnel_state = {}          # name -> {"up": bool, "error": str, "since": float}
_tstate_lock = threading.Lock()
_ui_lock = threading.Lock()


def load_cockpit_ui():
    try:
        with open(COCKPIT_UI_PATH) as f:
            ui = json.load(f)
    except (IOError, json.JSONDecodeError):
        ui = {}
    ui.setdefault("labels", {})   # session_id -> custom name
    ui.setdefault("order", [])    # session_id[] manual ordering
    return ui


def update_cockpit_ui(patch):
    """Merge a partial UI update (labels / order) and persist it."""
    with _ui_lock:
        ui = load_cockpit_ui()
        if "label" in patch:
            sid = patch.get("session_id", "")
            text = (patch.get("label") or "").strip()[:80]
            if sid:
                if text:
                    ui["labels"][sid] = text
                else:
                    ui["labels"].pop(sid, None)
        if "order" in patch and isinstance(patch["order"], list):
            ui["order"] = [s for s in patch["order"] if isinstance(s, str)]
        try:
            with open(COCKPIT_UI_PATH, "w") as f:
                json.dump(ui, f, indent=2)
        except IOError:
            pass
        return ui


def cockpit_hosts():
    try:
        with open(HOSTS_PATH) as f:
            return [h for h in json.load(f).get("hosts", []) if h.get("ssh")]
    except (IOError, json.JSONDecodeError):
        return []


def _set_tunnel(name, up, error=""):
    with _tstate_lock:
        prev = _tunnel_state.get(name, {})
        if prev.get("up") != up:
            prev["since"] = time.time()
        prev.update({"up": up, "error": error})
        prev.setdefault("since", time.time())
        _tunnel_state[name] = prev


def _tunnel_loop(host):
    """Keep one SSH local-forward alive; restart whenever it drops."""
    name = host.get("name", host["ssh"])
    lport = int(host["local_port"])
    cmd = [
        "ssh", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-L", f"127.0.0.1:{lport}:127.0.0.1:{LOCAL_COLLECTOR}",
        host["ssh"],
    ]
    while True:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            time.sleep(2)
            if proc.poll() is None:
                _set_tunnel(name, True)
            err = (proc.stderr.read() or b"").decode(errors="replace")[-200:] \
                if proc.stderr else ""
            proc.wait()
            _set_tunnel(name, False, err.strip() or "tunnel exited")
        except Exception as e:
            _set_tunnel(name, False, str(e))
        time.sleep(3)


def start_cockpit_tunnels():
    for host in cockpit_hosts():
        if host.get("local_port"):
            threading.Thread(target=_tunnel_loop, args=(host,), daemon=True).start()


def fetch_collector(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status",
                                    timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _launcher_dirs():
    """[(normalized_dir, name, color)] longest-first, from the launcher config —
    the single source of truth for a session's window identity/color."""
    out = []
    for p in load_config():
        d = p.get("directory")
        if not d:
            continue
        out.append((os.path.expanduser(d).rstrip("/"), p.get("name", ""),
                    p.get("background", "")))
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


def enrich(sess):
    """Give a session its window name/color by matching cwd to the launcher
    config, unless it already carried explicit identity from its shell."""
    cwd = (sess.get("cwd") or "").rstrip("/")
    if not sess.get("window_name") or not sess.get("window_color"):
        for norm, name, color in _launcher_dirs():
            if norm and (cwd == norm or cwd.startswith(norm + "/")):
                if not sess.get("window_name"):
                    sess["window_name"] = name
                if not sess.get("window_color"):
                    sess["window_color"] = color
                break
    return sess


def cockpit_live():
    """Merge local + every configured remote collector into one machine list."""
    machines = []
    local = fetch_collector(LOCAL_COLLECTOR)
    machines.append({
        "name": local.get("machine", "mac") if local else "mac",
        "label": "this mac",
        "reachable": local is not None,
        "error": "" if local else "local collector down",
        "sessions": [enrich(s) for s in local.get("sessions", [])] if local else [],
    })
    for host in cockpit_hosts():
        name = host.get("name", host["ssh"])
        with _tstate_lock:
            ts = dict(_tunnel_state.get(name, {}))
        data = fetch_collector(host["local_port"]) if ts.get("up") else None
        machines.append({
            "name": data.get("machine", name) if data else name,
            "label": host["ssh"],
            "reachable": data is not None,
            "error": "" if data else (ts.get("error") or "tunnel down"),
            "sessions": data.get("sessions", []) if data else [],
        })

    # Overlay user-set custom labels; hand the manual order to the client.
    ui = load_cockpit_ui()
    labels = ui.get("labels", {})
    for m in machines:
        for s in m.get("sessions", []):
            s["custom_title"] = labels.get(s.get("session_id", ""), "")
    return {"machines": machines, "order": ui.get("order", [])}


def load_status():
    """Pull the project-status repo (best effort) and return the card array."""
    try:
        subprocess.run(["git", "-C", STATUS_REPO, "pull", "--quiet", "--ff-only"],
                       capture_output=True, timeout=10)
    except Exception:
        pass  # offline / no repo — fall back to whatever is on disk
    try:
        with open(STATUS_JSON) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, FileNotFoundError):
        return []


def load_history(project, limit=30):
    """Return past versions of a project's card from git history (newest first)."""
    safe = os.path.basename(project)  # guard against path traversal
    if not safe:
        return []
    rel = f"status/{safe}.json"
    log = subprocess.run(
        ["git", "-C", STATUS_REPO, "log", "--format=%H", "--", rel],
        capture_output=True, text=True).stdout.strip()
    versions = []
    for sha in log.splitlines()[:limit]:
        show = subprocess.run(["git", "-C", STATUS_REPO, "show", f"{sha}:{rel}"],
                              capture_output=True, text=True)
        if show.returncode != 0:
            continue
        try:
            versions.append(json.loads(show.stdout))
        except json.JSONDecodeError:
            continue
    return versions

DEFAULT_PROJECTS = [
    {
        "name": "CXRO Website",
        "directory": "/Users/rhmiyakawa/Documents/Sites/cxro.lbl.gov/cxro-www-2026",
        "background": "#0d4d4d",
        "foreground": "#ffffff",
        "icon": "🌐"
    }
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return DEFAULT_PROJECTS.copy()


def save_config(projects):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(projects, f, indent=2)


def launch_ghostty(directory, background, foreground="#ffffff", ssh=None):
    directory = os.path.expanduser(directory)
    ghostty_bin = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
    cmd = [
        ghostty_bin,
        f"--background={background}",
        f"--foreground={foreground}",
    ]
    if ssh:
        # SSH into remote server
        cmd.extend(["-e", f"ssh {ssh}"])
    else:
        # Local directory
        cmd.append(f"--working-directory={directory}")
    try:
        subprocess.Popen(cmd, start_new_session=True)
        return True
    except Exception as e:
        print(f"Error launching ghostty: {e}")
        return False


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ghostty Launcher</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #e2e8f0;
            padding: 2rem;
        }

        .container { max-width: 1200px; margin: 0 auto; }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
        }

        h1 { font-size: 1.75rem; font-weight: 600; }

        .btn {
            padding: 0.6rem 1.2rem;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-primary {
            background: #8b5cf6;
            color: white;
        }
        .btn-primary:hover { background: #7c3aed; }

        .btn-secondary {
            background: rgba(255,255,255,0.1);
            color: #e2e8f0;
        }
        .btn-secondary:hover { background: rgba(255,255,255,0.2); }

        .cards {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 1.25rem;
        }

        .card {
            border-radius: 16px;
            padding: 1.5rem;
            cursor: pointer;
            transition: all 0.2s;
            border: 2px solid transparent;
            min-height: 160px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            position: relative;
        }

        .card:hover {
            transform: translateY(-4px);
            border-color: rgba(255,255,255,0.3);
            box-shadow: 0 12px 40px rgba(0,0,0,0.4);
        }

        .card-icon { font-size: 3rem; margin-bottom: 0.75rem; }
        .card-name { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.25rem; }
        .card-path {
            font-size: 0.75rem;
            opacity: 0.7;
            word-break: break-all;
            max-width: 100%;
        }

        .card-edit {
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(0,0,0,0.3);
            border: none;
            border-radius: 6px;
            padding: 4px 8px;
            cursor: pointer;
            opacity: 0;
            transition: opacity 0.2s;
            color: white;
            font-size: 0.8rem;
        }
        .card:hover .card-edit { opacity: 1; }
        .card-edit:hover { background: rgba(0,0,0,0.5); }

        .add-card {
            background: rgba(255,255,255,0.05);
            border: 2px dashed rgba(255,255,255,0.2);
            color: rgba(255,255,255,0.5);
        }
        .add-card:hover {
            background: rgba(255,255,255,0.1);
            border-color: rgba(255,255,255,0.4);
            color: rgba(255,255,255,0.8);
        }
        .add-card .card-icon { font-size: 2.5rem; }

        /* Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            z-index: 100;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.active { display: flex; }

        .modal {
            background: #1e293b;
            border-radius: 16px;
            padding: 2rem;
            width: 90%;
            max-width: 450px;
            box-shadow: 0 25px 50px rgba(0,0,0,0.5);
        }

        .modal h2 { margin-bottom: 1.5rem; font-size: 1.25rem; }

        .form-group { margin-bottom: 1.25rem; }
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            font-size: 0.875rem;
            font-weight: 500;
            color: #94a3b8;
        }

        .form-group input[type="text"] {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 1px solid #334155;
            border-radius: 8px;
            background: #0f172a;
            color: #e2e8f0;
            font-size: 1rem;
        }
        .form-group input:focus {
            outline: none;
            border-color: #8b5cf6;
        }

        .color-input-wrapper {
            display: flex;
            gap: 0.75rem;
            align-items: center;
        }

        .color-input-wrapper input[type="color"] {
            width: 50px;
            height: 42px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            background: none;
        }

        .color-input-wrapper input[type="text"] {
            flex: 1;
        }

        .modal-actions {
            display: flex;
            gap: 0.75rem;
            justify-content: flex-end;
            margin-top: 1.5rem;
        }

        .btn-danger {
            background: #dc2626;
            color: white;
            margin-right: auto;
        }
        .btn-danger:hover { background: #b91c1c; }

        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: #64748b;
        }
        .empty-state p { margin-bottom: 1rem; }

        /* Tabs */
        .tabs { display: flex; gap: 0.5rem; }
        .tab {
            padding: 0.5rem 1.1rem;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            background: rgba(255,255,255,0.08);
            color: #94a3b8;
            transition: all 0.2s;
        }
        .tab:hover { background: rgba(255,255,255,0.15); color: #e2e8f0; }
        .tab.active { background: #8b5cf6; color: white; }

        /* Status view */
        .status-bar {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }
        .status-updated { font-size: 0.75rem; color: #64748b; }
        .status-refresh { font-size: 0.8rem; padding: 0.45rem 0.9rem; }
        .status-refresh.spinning { opacity: 0.6; pointer-events: none; }
        .status-layout {
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
        }
        .status-list {
            display: flex;
            flex-direction: row;
            flex-wrap: wrap;
            gap: 0.6rem;
            padding-bottom: 1.25rem;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .status-item {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.55rem 0.95rem;
            border-radius: 999px;
            cursor: pointer;
            background: rgba(255,255,255,0.05);
            border: 1px solid transparent;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .status-item:hover { background: rgba(255,255,255,0.1); }
        .status-item.active { background: rgba(139,92,246,0.18); border-color: #8b5cf6; }
        .status-item .glyph { font-size: 0.85rem; }
        .status-item .si-name { font-weight: 600; font-size: 0.9rem; }
        .status-item .si-age { font-size: 0.68rem; opacity: 0.55; }

        .status-detail {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 1.75rem;
            min-height: 300px;
        }
        .status-empty { color: #64748b; text-align: center; padding: 3rem 1rem; }
        .sd-head { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
        .sd-head h2 { font-size: 1.4rem; }
        .sd-badge {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding: 0.2rem 0.6rem;
            border-radius: 999px;
        }
        .sd-meta {
            font-size: 0.78rem;
            color: #94a3b8;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            margin-bottom: 1.25rem;
            word-break: break-all;
        }
        .sd-fields { display: flex; flex-direction: column; gap: 0.75rem; margin-bottom: 1.5rem; }
        .sd-field { display: grid; grid-template-columns: 70px 1fr; gap: 0.75rem; }
        .sd-field .k { font-size: 0.75rem; color: #64748b; text-transform: uppercase; padding-top: 2px; }
        .sd-field .v { font-size: 0.95rem; }
        .sd-details {
            border-top: 1px solid rgba(255,255,255,0.08);
            padding-top: 1.25rem;
            font-size: 0.9rem;
            line-height: 1.6;
            color: #cbd5e1;
        }
        .sd-details code {
            background: rgba(0,0,0,0.35);
            padding: 0.1rem 0.35rem;
            border-radius: 5px;
            font-size: 0.85em;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        }
        .sd-details strong { color: #e2e8f0; }
        .sd-launch { margin-top: 1.5rem; }

        /* Status history */
        .sd-hist-head {
            margin-top: 1.5rem;
            padding-top: 1rem;
            border-top: 1px solid rgba(255,255,255,0.08);
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #64748b;
            margin-bottom: 0.6rem;
        }
        .sd-hist-item {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 8px;
            margin-bottom: 0.4rem;
            padding: 0.5rem 0.75rem;
        }
        .sd-hist-item summary {
            cursor: pointer;
            display: flex;
            gap: 0.6rem;
            align-items: center;
            font-size: 0.85rem;
            list-style: none;
        }
        .sd-hist-item summary::-webkit-details-marker { display: none; }
        .sd-hist-item summary:hover { color: #fff; }
        .sd-hist-when { color: #94a3b8; font-size: 0.72rem; white-space: nowrap; }
        .sd-hist-focus { color: #cbd5e1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sd-hist-body {
            margin-top: 0.6rem;
            padding-top: 0.6rem;
            border-top: 1px solid rgba(255,255,255,0.06);
            font-size: 0.85rem;
            color: #cbd5e1;
        }
        .sd-hist-line { margin-bottom: 0.3rem; }
        .sd-hist-line .k {
            color: #64748b;
            text-transform: uppercase;
            font-size: 0.7rem;
            margin-right: 0.4rem;
        }
        .sd-hist-details { margin-top: 0.5rem; line-height: 1.55; }
        .sd-hist-details code {
            background: rgba(0,0,0,0.35);
            padding: 0.1rem 0.35rem;
            border-radius: 5px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.85em;
        }

        /* ---- Cockpit (live agent status) ---- */
        .cockpit-bar { display:flex; align-items:center; justify-content:flex-end;
                       gap:.75rem; margin-bottom:1rem; min-height:1.2rem; }
        .cockpit-bar .sub { font-size:.8rem; color:#94a3b8; }
        .cockpit-bar .hint { margin-right:auto; font-size:.72rem; color:#64748b; }
        .machines { display:flex; flex-direction:column; gap:1.1rem; }
        .machine-head { display:flex; align-items:center; gap:.55rem; margin-bottom:.55rem;
                        font-size:.8rem; text-transform:uppercase; letter-spacing:.06em; color:#94a3b8; }
        .machine-head .mdot { width:.55rem; height:.55rem; border-radius:50%; }
        .machine-head .mlabel { font-family:ui-monospace,Menlo,monospace; text-transform:none;
                                letter-spacing:0; opacity:.55; font-size:.72rem; }
        .machine-head .merr { color:#b5707c; text-transform:none; letter-spacing:0; font-size:.72rem; }
        .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:.75rem; }
        /* Desaturated full outline conveys state; no bright dots. */
        .scard { background:rgba(255,255,255,0.035); border:1.5px solid rgba(255,255,255,0.10);
                 border-radius:12px; padding:.9rem 1rem; position:relative;
                 min-height:172px; display:flex; flex-direction:column;
                 cursor:grab; transition:border-color .15s, box-shadow .15s, opacity .15s; }
        .scard:active { cursor:grabbing; }
        .scard.dragging { opacity:.4; }
        .scard.dragover { box-shadow:0 0 0 2px rgba(148,163,184,.5); }
        .scard .st { display:flex; align-items:center; gap:.5rem; margin-bottom:.45rem; }
        .scard .sdot { width:.55rem; height:.55rem; border-radius:50%; flex:none; }
        .scard .state { font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
        .scard .subs { font-size:.64rem; color:#cbd5e1; background:rgba(255,255,255,0.08);
                       padding:.05rem .4rem; border-radius:999px; }
        .scard .ctx { font-size:.64rem; color:#8b98a5; background:rgba(255,255,255,0.05);
                      padding:.05rem .4rem; border-radius:999px; font-family:ui-monospace,Menlo,monospace; }
        .scard .age { margin-left:auto; font-size:.68rem; color:#64748b; }
        .scard .proj { font-size:1.0rem; font-weight:650; margin-bottom:.25rem;
                       display:flex; align-items:center; gap:.42rem; }
        .scard .swatch { width:.72rem; height:.72rem; border-radius:3px; flex:none;
                         box-shadow:0 0 0 1px rgba(255,255,255,0.18) inset; }
        .scard .idtag { font-size:.58rem; color:#5b6773; font-family:ui-monospace,Menlo,monospace;
                        margin-left:auto; }
        .scard .title { font-size:.8rem; color:#e2e8f0; opacity:.9; margin-bottom:.3rem;
                        border-radius:5px; padding:.1rem .25rem; margin-left:-.25rem;
                        outline:none; cursor:text; }
        .scard .title:empty::before { content:'+ label'; color:#5b6773; font-style:italic; }
        .scard .title:hover { background:rgba(255,255,255,0.05); }
        .scard .title:focus { background:rgba(255,255,255,0.09);
                              box-shadow:0 0 0 1px rgba(148,163,184,.4); }
        .scard .activity { font-size:.75rem; color:#9aa7b4; line-height:1.42; margin-bottom:.4rem;
                           display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical;
                           overflow:hidden; flex:1; }
        .scard .foot { margin-top:auto; }
        .scard .detail { font-size:.7rem; color:#64748b; overflow:hidden;
                         text-overflow:ellipsis; white-space:nowrap; }
        .scard .cwd { margin-top:.3rem; font-size:.64rem; color:#55606c;
                      font-family:ui-monospace,Menlo,monospace;
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        /* Desaturated state palette (border + label + dot). */
        .scard.s-need    { border-color:rgba(176,110,124,.85); }
        .scard.s-need    .state, .scard.s-need .sdot { color:#c08794; }
        .scard.s-need    .sdot { background:#b06e7c; }
        .scard.s-working { border-color:rgba(108,150,124,.85); }
        .scard.s-working .state { color:#7faa8f; }
        .scard.s-working .sdot { background:#6f9e80; }
        .scard.s-done    { border-color:rgba(108,138,168,.85); }
        .scard.s-done    .state { color:#87a2bd; }
        .scard.s-done    .sdot { background:#7291ab; }
        .scard.s-starting{ border-color:rgba(138,124,168,.85); }
        .scard.s-starting .state { color:#a294c0; }
        .scard.s-starting .sdot { background:#8a7ca8; }
        .scard.s-stale   { border-color:rgba(90,100,114,.7); opacity:.55; }
        .scard.s-stale   .state { color:#7a8492; }
        .scard.s-stale   .sdot { background:#5a6472; }
        /* Gentle, desaturated attention pulse for "needs you" only. */
        .scard.s-need { animation:sglow 2.4s ease-in-out infinite; }
        @keyframes sglow { 0%,100%{box-shadow:0 0 0 0 rgba(176,110,124,0);}
            50%{box-shadow:0 0 0 3px rgba(176,110,124,.18);} }
        .work-pip { width:.55rem; height:.55rem; border-radius:50%; background:#6f9e80;
                    flex:none; animation:sblink 1.4s ease-in-out infinite; }
        @keyframes sblink { 0%,100%{opacity:1;} 50%{opacity:.3;} }
        .machines .empty { color:#64748b; font-size:.85rem; padding:.3rem 0; }
        .machines .none { color:#64748b; text-align:center; padding:4rem 1rem; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🖥️ Ghostty Launcher</h1>
            <nav class="tabs">
                <button class="tab active" id="tab-cockpit" onclick="switchView('cockpit')">🛩️ Cockpit</button>
                <button class="tab" id="tab-launcher" onclick="switchView('launcher')">Launcher</button>
                <button class="tab" id="tab-status" onclick="switchView('status')">History</button>
            </nav>
        </header>

        <div id="view-cockpit">
            <div class="cockpit-bar">
                <span class="hint">Click a label to rename · drag cards to reorder</span>
                <span class="sub" id="cockpit-sub"></span>
            </div>
            <div class="machines" id="machines"><div class="none">Connecting…</div></div>
        </div>

        <div id="view-launcher" style="display:none">
            <div class="cards" id="cards"></div>
        </div>

        <div id="view-status" style="display:none">
            <div class="status-bar">
                <span class="status-updated" id="status-updated"></span>
                <button class="btn btn-secondary status-refresh" id="status-refresh" onclick="loadStatus(true)">↻ Refresh</button>
            </div>
            <div class="status-layout">
                <div class="status-list" id="status-list"></div>
                <div class="status-detail" id="status-detail">
                    <div class="status-empty">Select a project to view its status.</div>
                </div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="modal">
        <div class="modal">
            <h2 id="modal-title">Add Project</h2>
            <form id="project-form">
                <input type="hidden" id="edit-index" value="-1">

                <div class="form-group">
                    <label>Name</label>
                    <input type="text" id="project-name" placeholder="My Project" required>
                </div>

                <div class="form-group">
                    <label>Icon (emoji)</label>
                    <input type="text" id="project-icon" placeholder="📁" maxlength="4">
                </div>

                <div class="form-group">
                    <label>Directory Path (local)</label>
                    <input type="text" id="project-directory" placeholder="/path/to/project">
                </div>

                <div class="form-group">
                    <label>SSH Command (optional, overrides directory)</label>
                    <input type="text" id="project-ssh" placeholder="user@hostname">
                </div>

                <div class="form-group">
                    <label>Background Color</label>
                    <div class="color-input-wrapper">
                        <input type="color" id="project-bg-picker" value="#1a1a2e">
                        <input type="text" id="project-bg" placeholder="#1a1a2e" required>
                    </div>
                </div>

                <div class="form-group">
                    <label>Foreground Color</label>
                    <div class="color-input-wrapper">
                        <input type="color" id="project-fg-picker" value="#ffffff">
                        <input type="text" id="project-fg" placeholder="#ffffff" required>
                    </div>
                </div>

                <div class="modal-actions">
                    <button type="button" class="btn btn-danger" id="delete-btn" style="display:none">Delete</button>
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        let projects = [];

        async function loadProjects() {
            try {
                const res = await fetch('/api/projects');
                projects = await res.json();
                console.log('Loaded projects:', projects);
            } catch (err) {
                console.error('Failed to load projects:', err);
                projects = [];
            }
            renderCards();
        }

        function renderCards() {
            const container = document.getElementById('cards');
            console.log('renderCards called, projects:', projects, 'container:', container);

            if (!container) {
                console.error('Cards container not found!');
                return;
            }

            if (!projects || projects.length === 0) {
                container.innerHTML = `
                    <div class="card add-card" onclick="openAddModal()">
                        <div class="card-icon">+</div>
                        <div class="card-name">Add Project</div>
                    </div>
                `;
                return;
            }

            container.innerHTML = projects.map((p, i) => `
                <div class="card" style="background: ${p.background}; color: ${p.foreground || '#ffffff'}" onclick="launch(${i})">
                    <button class="card-edit" onclick="event.stopPropagation(); openEditModal(${i})">Edit</button>
                    <div class="card-icon">${p.icon || '📁'}</div>
                    <div class="card-name">${escapeHtml(p.name)}</div>
                    <div class="card-path">${p.ssh ? 'SSH: ' + escapeHtml(p.ssh) : escapeHtml(truncatePath(p.directory || ''))}</div>
                </div>
            `).join('') + `
                <div class="card add-card" onclick="openAddModal()">
                    <div class="card-icon">+</div>
                    <div class="card-name">Add Project</div>
                </div>
            `;
        }

        function truncatePath(path) {
            if (path.length > 35) return '...' + path.slice(-32);
            return path;
        }

        function escapeHtml(str) {
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }

        async function launch(index) {
            const p = projects[index];
            await fetch('/api/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    directory: p.directory,
                    background: p.background,
                    foreground: p.foreground || '#ffffff',
                    ssh: p.ssh || null
                })
            });
        }

        function openAddModal() {
            document.getElementById('modal-title').textContent = 'Add Project';
            document.getElementById('edit-index').value = -1;
            document.getElementById('project-name').value = '';
            document.getElementById('project-icon').value = '📁';
            document.getElementById('project-directory').value = '';
            document.getElementById('project-ssh').value = '';
            document.getElementById('project-bg').value = '#1a1a2e';
            document.getElementById('project-bg-picker').value = '#1a1a2e';
            document.getElementById('project-fg').value = '#ffffff';
            document.getElementById('project-fg-picker').value = '#ffffff';
            document.getElementById('delete-btn').style.display = 'none';
            document.getElementById('modal').classList.add('active');
        }

        function openEditModal(index) {
            const p = projects[index];
            document.getElementById('modal-title').textContent = 'Edit Project';
            document.getElementById('edit-index').value = index;
            document.getElementById('project-name').value = p.name;
            document.getElementById('project-icon').value = p.icon || '📁';
            document.getElementById('project-directory').value = p.directory || '';
            document.getElementById('project-ssh').value = p.ssh || '';
            document.getElementById('project-bg').value = p.background;
            document.getElementById('project-bg-picker').value = p.background;
            document.getElementById('project-fg').value = p.foreground || '#ffffff';
            document.getElementById('project-fg-picker').value = p.foreground || '#ffffff';
            document.getElementById('delete-btn').style.display = 'block';
            document.getElementById('modal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('active');
        }

        document.getElementById('project-bg-picker').addEventListener('input', (e) => {
            document.getElementById('project-bg').value = e.target.value;
        });

        document.getElementById('project-bg').addEventListener('input', (e) => {
            if (/^#[0-9a-fA-F]{6}$/.test(e.target.value)) {
                document.getElementById('project-bg-picker').value = e.target.value;
            }
        });

        document.getElementById('project-fg-picker').addEventListener('input', (e) => {
            document.getElementById('project-fg').value = e.target.value;
        });

        document.getElementById('project-fg').addEventListener('input', (e) => {
            if (/^#[0-9a-fA-F]{6}$/.test(e.target.value)) {
                document.getElementById('project-fg-picker').value = e.target.value;
            }
        });

        document.getElementById('project-form').addEventListener('submit', async (e) => {
            e.preventDefault();

            const ssh = document.getElementById('project-ssh').value.trim();
            const directory = document.getElementById('project-directory').value.trim();

            if (!ssh && !directory) {
                alert('Please specify either a directory or SSH command');
                return;
            }

            const index = parseInt(document.getElementById('edit-index').value);
            const project = {
                name: document.getElementById('project-name').value,
                icon: document.getElementById('project-icon').value || '📁',
                directory: directory,
                ssh: ssh || null,
                background: document.getElementById('project-bg').value,
                foreground: document.getElementById('project-fg').value || '#ffffff'
            };

            if (index >= 0) {
                projects[index] = project;
            } else {
                projects.push(project);
            }

            await saveProjects();
            closeModal();
            renderCards();
        });

        document.getElementById('delete-btn').addEventListener('click', async () => {
            const index = parseInt(document.getElementById('edit-index').value);
            if (index >= 0 && confirm('Delete this project?')) {
                projects.splice(index, 1);
                await saveProjects();
                closeModal();
                renderCards();
            }
        });

        async function saveProjects() {
            await fetch('/api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(projects)
            });
        }

        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') closeModal();
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeModal();
        });

        // ---- Status view ----
        const STATUS_UI = {
            blocked: { glyph: '🔴', label: 'blocked', color: '#dc2626' },
            active:  { glyph: '🟢', label: 'active',  color: '#16a34a' },
            paused:  { glyph: '🟡', label: 'paused',  color: '#ca8a04' },
            done:    { glyph: '✅', label: 'done',    color: '#3b82f6' },
        };
        let statusCards = [];
        let statusSelected = -1;
        let statusLoaded = false;

        function switchView(name) {
            for (const v of ['cockpit', 'launcher', 'status']) {
                document.getElementById('view-' + v).style.display = (v === name) ? '' : 'none';
                document.getElementById('tab-' + v).classList.toggle('active', v === name);
            }
            if (name === 'status' && !statusLoaded) loadStatus();
        }

        async function loadStatus(manual) {
            statusLoaded = true;
            const btn = document.getElementById('status-refresh');
            // Remember which project is selected so a refresh doesn't lose it.
            const prevName = statusSelected >= 0 && statusCards[statusSelected]
                ? statusCards[statusSelected].project : null;
            if (manual && btn) btn.classList.add('spinning');
            if (!statusCards.length) {
                document.getElementById('status-list').innerHTML =
                    '<div class="status-empty">Loading…</div>';
            }
            try {
                const res = await fetch('/api/status');
                statusCards = await res.json();
            } catch (err) {
                statusCards = [];
            } finally {
                if (btn) btn.classList.remove('spinning');
            }
            // Most recently updated first.
            statusCards.sort((a, b) => (Date.parse(b.updated) || 0) - (Date.parse(a.updated) || 0));
            // Restore the previous selection by project name (index may have shifted).
            statusSelected = prevName
                ? statusCards.findIndex(c => c.project === prevName) : -1;
            renderStatusList();
            if (statusSelected >= 0) selectStatus(statusSelected);
            const upd = document.getElementById('status-updated');
            if (upd) upd.textContent = 'Updated ' + new Date().toLocaleTimeString();
        }

        function ago(iso) {
            const t = Date.parse(iso);
            if (isNaN(t)) return iso || '';
            const s = (Date.now() - t) / 1000;
            if (s < 3600) return Math.floor(s / 60) + 'm ago';
            if (s < 86400) return Math.floor(s / 3600) + 'h ago';
            return Math.floor(s / 86400) + 'd ago';
        }

        function renderStatusList() {
            const list = document.getElementById('status-list');
            if (!statusCards.length) {
                list.innerHTML = '<div class="status-empty">No status cards found.<br>' +
                    'Run "save state" in a project.</div>';
                return;
            }
            list.innerHTML = statusCards.map((c, i) => {
                const ui = STATUS_UI[c.status] || { glyph: '⚪' };
                return `<div class="status-item ${i === statusSelected ? 'active' : ''}" onclick="selectStatus(${i})">
                    <span class="glyph">${ui.glyph}</span>
                    <span class="si-name">${escapeHtml(c.project || '?')}</span>
                    <span class="si-age">${ago(c.updated)}</span>
                </div>`;
            }).join('');
        }

        function renderDetails(md) {
            let h = escapeHtml(md || '');
            h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
            h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
            h = h.replace(/\n/g, '<br>');
            return h;
        }

        function field(k, v) {
            if (!v) return '';
            return `<div class="sd-field"><div class="k">${k}</div><div class="v">${escapeHtml(v)}</div></div>`;
        }

        function selectStatus(i) {
            statusSelected = i;
            renderStatusList();
            const c = statusCards[i];
            const ui = STATUS_UI[c.status] || { glyph: '⚪', label: c.status, color: '#64748b' };
            const detail = document.getElementById('status-detail');
            detail.innerHTML = `
                <div class="sd-head">
                    <span>${ui.glyph}</span>
                    <h2>${escapeHtml(c.project || '?')}</h2>
                    <span class="sd-badge" style="background:${ui.color};color:#fff">${ui.label}</span>
                </div>
                <div class="sd-meta">${escapeHtml(c.machine || '')} · ${escapeHtml(c.pwd || '')} · ${escapeHtml(c.repo || '')} @ ${escapeHtml(c.branch || '')} · updated ${ago(c.updated)}</div>
                <div class="sd-fields">
                    ${field('Focus', c.focus)}
                    ${field('Blocker', c.blocker)}
                    ${field('Next', c.next)}
                </div>
                ${c.details ? `<div class="sd-details">${renderDetails(c.details)}</div>` : ''}
                <div class="sd-launch">
                    <button class="btn btn-secondary" onclick="launchStatus(${i})">🖥️ Open in Ghostty</button>
                </div>
                <div id="sd-history"></div>
            `;
            loadHistory(c.project);
        }

        async function loadHistory(project) {
            const el = document.getElementById('sd-history');
            if (!el) return;
            el.innerHTML = '<div class="sd-hist-head">Loading history…</div>';
            let hist = [];
            try {
                const r = await fetch('/api/history?project=' + encodeURIComponent(project));
                hist = await r.json();
            } catch (e) { el.innerHTML = ''; return; }
            const past = hist.slice(1);  // [0] is the current card, already shown above
            if (!past.length) { el.innerHTML = '<div class="sd-hist-head">No earlier saves</div>'; return; }
            el.innerHTML = '<div class="sd-hist-head">History · ' + past.length +
                ' earlier save' + (past.length > 1 ? 's' : '') + '</div>' +
                past.map(c => {
                    const ui = STATUS_UI[c.status] || { glyph: '⚪' };
                    return `<details class="sd-hist-item">
                        <summary><span>${ui.glyph}</span><span class="sd-hist-when">${ago(c.updated)}</span><span class="sd-hist-focus">${escapeHtml(c.focus || '')}</span></summary>
                        <div class="sd-hist-body">
                            ${c.blocker ? '<div class="sd-hist-line"><span class="k">Blocker</span> ' + escapeHtml(c.blocker) + '</div>' : ''}
                            ${c.next ? '<div class="sd-hist-line"><span class="k">Next</span> ' + escapeHtml(c.next) + '</div>' : ''}
                            ${c.details ? '<div class="sd-hist-details">' + renderDetails(c.details) + '</div>' : ''}
                            <div class="sd-hist-line"><span class="k">Branch</span> ${escapeHtml(c.branch || '')}</div>
                        </div>
                    </details>`;
                }).join('');
        }

        async function launchStatus(i) {
            const c = statusCards[i];
            await fetch('/api/launch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ directory: c.pwd || '~', background: '#16213e', foreground: '#ffffff' })
            });
        }

        // Silently refresh the status view every 2 minutes (only when it's visible
        // and the tab is focused, to avoid needless git pulls in the background).
        setInterval(() => {
            const visible = document.getElementById('view-status').style.display !== 'none';
            if (visible && !document.hidden) loadStatus(false);
        }, 120000);

        // ---- Cockpit (live agent status) ----
        const CUI = {
          needs_input:{l:'needs you',c:'need'},
          working:{l:'working',c:'working'},
          done:{l:'done',c:'done'},
          starting:{l:'starting',c:'starting'},
          stale:{l:'stale',c:'stale'},
          idle:{l:'done',c:'done'},
        };
        const CORDER = {needs_input:0, working:1, starting:2, done:3, idle:3, stale:4};
        let cockpitTimer = null;
        let manualOrder = [];   // session_id[] — user's drag order
        let cpBusy = false;     // editing a label or dragging → pause re-render

        function agoSec(a){ if(a==null) return ''; a=Math.max(0,a|0);
          if(a<60) return a+'s'; if(a<3600) return (a/60|0)+'m'; return (a/3600|0)+'h'; }
        function fmtTok(n){ if(!n) return '';
          return n>=1000 ? (n/1000).toFixed(n>=100000?0:1).replace(/\.0$/,'')+'k' : ''+n; }
        function shortModel(m){ return (m||'').replace(/^claude-/,'').replace(/-\d{6,}$/,''); }
        function orderIndex(sid){ const i = manualOrder.indexOf(sid); return i<0 ? 1e6 : i; }

        async function saveUI(patch){
          try {
            const r = await fetch('/api/ui', {method:'POST',
              headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
            const ui = await r.json();
            if(ui && ui.order) manualOrder = ui.order;
          } catch(e){}
        }

        async function cockpitTick(){
          if(cpBusy) return;   // never clobber an in-progress edit / drag
          let data;
          try { data = await (await fetch('/api/live')).json(); }
          catch(e){ return; }
          const machines = data.machines || [];
          manualOrder = data.order || [];
          let need=0, work=0, total=0, html='';
          for(const m of machines){
            const sess = (m.sessions||[]).slice().sort((a,b)=>{
              const oa=orderIndex(a.session_id), ob=orderIndex(b.session_id);
              return oa!==ob ? oa-ob : (CORDER[a.state]??9)-(CORDER[b.state]??9);
            });
            total += sess.length;
            need += sess.filter(s=>s.state==='needs_input').length;
            work += sess.filter(s=>s.state==='working').length;
            const mdot = m.reachable ? '#6f9e80' : '#b06e7c';
            html += `<div><div class="machine-head">
                <span class="mdot" style="background:${mdot}"></span>
                <span>${escapeHtml(m.name)}</span>
                <span class="mlabel">${escapeHtml(m.label||'')}</span>
                ${m.reachable?'':`<span class="merr">· ${escapeHtml(m.error||'unreachable')}</span>`}
              </div>`;
            if(!sess.length){
              html += `<div class="empty">${m.reachable?'no active sessions':'—'}</div></div>`;
              continue;
            }
            html += '<div class="grid">';
            for(const s of sess){
              const ui = CUI[s.state] || {l:s.state, c:'stale'};
              const marker = s.state==='working'
                ? '<span class="work-pip"></span>' : '<span class="sdot"></span>';
              const name = s.window_name || s.project || '?';
              const swatch = s.window_color
                ? `<span class="swatch" style="background:${escapeHtml(s.window_color)}"></span>` : '';
              const subs = (s.subagents>0)
                ? `<span class="subs">▷ ${s.subagents} sub${s.subagents>1?'s':''}</span>` : '';
              const ctx = s.context_tokens
                ? `<span class="ctx" title="context tokens / model">${fmtTok(s.context_tokens)}${s.model?(' · '+escapeHtml(shortModel(s.model))):''}</span>` : '';
              const idtag = `<span class="idtag">${escapeHtml((s.session_id||'').slice(0,6))}</span>`;
              const label = s.custom_title || s.title || '';
              const activity = `<div class="activity">${escapeHtml(s.activity||'')}</div>`;
              html += `<div class="scard s-${ui.c}" draggable="true" data-sid="${escapeHtml(s.session_id)}">
                <div class="st">${marker}<span class="state">${ui.l}</span>${subs}${ctx}<span class="age">${agoSec(s.age)}</span></div>
                <div class="proj">${swatch}${escapeHtml(name)}${idtag}</div>
                <div class="title" contenteditable="true" spellcheck="false" data-sid="${escapeHtml(s.session_id)}">${escapeHtml(label)}</div>
                ${activity}
                <div class="foot">
                  <div class="detail">${escapeHtml(s.detail||'')||'&nbsp;'}</div>
                  <div class="cwd">${escapeHtml(s.cwd||'')}</div>
                </div>
              </div>`;
            }
            html += '</div></div>';
          }
          document.getElementById('machines').innerHTML = total ? html :
            '<div class="none">No sessions reporting yet.<br>Start a Claude session on any wired machine.</div>';
          wireCockpitCards();
          const bits = [];
          if(need) bits.push(need+' need you');
          if(work) bits.push(work+' working');
          bits.push(total+' total');
          document.getElementById('cockpit-sub').textContent =
            bits.join(' · ') + ' · ' + new Date().toLocaleTimeString();
          document.title = (need ? `(${need}!) ` : '') + 'Ghostty Launcher';
        }

        function persistOrder(){
          const sids = [...document.querySelectorAll('.scard')].map(c=>c.dataset.sid);
          manualOrder = sids;
          saveUI({order: sids});
        }

        let dragEl = null;
        function wireCockpitCards(){
          // Editable labels — click, type, Enter/blur to save.
          document.querySelectorAll('.scard .title').forEach(el=>{
            const card = el.closest('.scard');
            el.addEventListener('focus', ()=>{ cpBusy = true; card.draggable = false; });
            el.addEventListener('blur', ()=>{
              cpBusy = false; card.draggable = true;
              saveUI({session_id: el.dataset.sid, label: el.textContent});
            });
            el.addEventListener('keydown', e=>{
              if(e.key==='Enter' || e.key==='Escape'){ e.preventDefault(); el.blur(); }
            });
          });
          // Drag to reorder.
          document.querySelectorAll('.scard').forEach(card=>{
            card.addEventListener('dragstart', e=>{
              dragEl = card; cpBusy = true; card.classList.add('dragging');
              e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', ()=>{
              card.classList.remove('dragging');
              document.querySelectorAll('.scard.dragover').forEach(c=>c.classList.remove('dragover'));
              dragEl = null; cpBusy = false; persistOrder();
            });
            card.addEventListener('dragover', e=>{
              e.preventDefault();
              if(!dragEl || dragEl===card) return;
              const grid = card.parentElement;
              const rect = card.getBoundingClientRect();
              const after = (e.clientY - rect.top) / rect.height > 0.5;
              grid.insertBefore(dragEl, after ? card.nextSibling : card);
            });
          });
        }

        function startCockpit(){
          if(!cockpitTimer){ cockpitTick(); cockpitTimer = setInterval(cockpitTick, 1500); }
        }

        // Cockpit is the default view; launcher data preloads in the background.
        renderCards();
        loadProjects();
        startCockpit();
    </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress request logging

    def _send_response(self, content, content_type="text/html", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if isinstance(content, bytes):
            data = content
        else:
            data = content.encode('utf-8')
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_response(HTML_TEMPLATE)
        elif self.path == "/api/projects":
            self._send_response(json.dumps(load_config()), "application/json")
        elif self.path == "/api/status":
            self._send_response(json.dumps(load_status()), "application/json")
        elif self.path == "/api/live":
            self._send_response(json.dumps(cockpit_live()), "application/json")
        elif self.path.startswith("/api/history"):
            q = parse_qs(urlparse(self.path).query)
            self._send_response(json.dumps(load_history(q.get("project", [""])[0])),
                                "application/json")
        else:
            self._send_response("Not Found", status=404)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode()

        if self.path == "/api/projects":
            projects = json.loads(body)
            save_config(projects)
            self._send_response('{"ok":true}', "application/json")

        elif self.path == "/api/launch":
            data = json.loads(body)
            success = launch_ghostty(
                data.get("directory", "~"),
                data["background"],
                data.get("foreground", "#ffffff"),
                data.get("ssh")
            )
            self._send_response(json.dumps({"ok": success}), "application/json")

        elif self.path == "/api/ui":
            ui = update_cockpit_ui(json.loads(body))
            self._send_response(json.dumps(ui), "application/json")

        else:
            self._send_response("Not Found", status=404)


def main():
    start_cockpit_tunnels()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Ghostty Launcher running at {url}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
