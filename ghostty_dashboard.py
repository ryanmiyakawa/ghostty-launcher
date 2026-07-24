#!/usr/bin/env python3
"""
Ghostty Terminal Launcher Dashboard - Web UI
A browser-based launcher with configuration and color picker.
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

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
    ui.setdefault("labels", {})     # session_id -> custom note label
    ui.setdefault("order", [])      # session_id[] manual ordering
    ui.setdefault("identity", {})   # directory -> {name, color} window identity override
    ui.setdefault("tunnels", {})    # host name -> {"enabled": true}; absent = OFF
    return ui


def tunnel_enabled(ui, name):
    """Tunnels are OPT-IN (greenlight model): no persistent ssh to a host until
    the user explicitly enables it. An absent entry means paused."""
    ent = (ui.get("tunnels") or {}).get(name)
    return isinstance(ent, dict) and ent.get("enabled") is True


def _is_hex_color(s):
    return isinstance(s, str) and len(s) == 7 and s[0] == "#" and \
        all(c in "0123456789abcdefABCDEF" for c in s[1:])


def update_cockpit_ui(patch):
    """Merge a partial UI update (labels / order / identity) and persist it."""
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
        if "tunnel" in patch and isinstance(patch["tunnel"], dict):
            # Per-host tunnel greenlight, persisted across dashboard restarts.
            # Opt-in: enabling writes {"enabled": true}; disabling removes the
            # entry (absent = off, the default).
            name = (patch["tunnel"].get("name") or "").strip()[:80]
            if name:
                if patch["tunnel"].get("enabled") is True:
                    ui["tunnels"][name] = {"enabled": True}
                else:
                    ui["tunnels"].pop(name, None)
        if "identity" in patch:
            # Window name/color override, keyed by directory so it sticks across
            # relaunches and covers every session under that dir.
            cwd = (patch.get("cwd") or "").rstrip("/")
            if cwd:
                ent = ui["identity"].get(cwd, {})
                if "name" in patch:
                    name = (patch.get("name") or "").strip()[:60]
                    if name:
                        ent["name"] = name
                    else:
                        ent.pop("name", None)
                if "color" in patch:
                    color = (patch.get("color") or "").strip()
                    if _is_hex_color(color):
                        ent["color"] = color
                    elif color == "":
                        ent.pop("color", None)
                if ent:
                    ui["identity"][cwd] = ent
                else:
                    ui["identity"].pop(cwd, None)
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


# Per-host tunnel control: a "run" Event the worker parks on when the host is
# paused, plus the live ssh child so a pause can terminate it immediately.
_tunnel_ctl = {}  # name -> {"run": threading.Event, "proc": Popen | None}


def _tunnel_paused(name):
    ctl = _tunnel_ctl.get(name)
    return bool(ctl) and not ctl["run"].is_set()


def _tunnel_loop(host):
    """Keep one SSH local-forward alive while the host is enabled. Paused →
    terminate the child and park on the run event. Down-but-enabled → retry
    with exponential backoff (3s → 60s, reset on success) so an unreachable
    host (off VPN, machine gone) isn't hammered forever."""
    name = host.get("name", host["ssh"])
    ctl = _tunnel_ctl[name]
    lport = int(host["local_port"])
    cmd = [
        "ssh", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        # Never multiplex: with a user ControlMaster auto+ControlPersist config
        # our tunnel would daemonize as (or ride) a mux master — the child we
        # watch exits, liveness tracking lies, and the forward outlives us as
        # an unkillable PPID-1 orphan. Own connection, own forward, always.
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-L", f"127.0.0.1:{lport}:127.0.0.1:{LOCAL_COLLECTOR}",
        host["ssh"],
    ]
    backoff = 3
    while True:
        if _shutting_down:
            return
        if not ctl["run"].is_set():
            _set_tunnel(name, False, "")
            ctl["run"].wait()
            backoff = 3
            continue
        proc = None
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            ctl["proc"] = proc
            time.sleep(2)
            if proc.poll() is None:
                _set_tunnel(name, True)
            # Watch the child; a pause mid-flight terminates it promptly.
            while proc.poll() is None:
                if not ctl["run"].is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(0.5)
            err = (proc.stderr.read() or b"").decode(errors="replace")[-200:] \
                if proc.stderr else ""
            proc.wait()
            _set_tunnel(name, False, "" if not ctl["run"].is_set()
                        else (err.strip() or "tunnel exited"))
            # Self-heal a stale forward: an orphaned ssh (e.g. from a previous
            # dashboard that died without cleanup) squatting on our local port
            # makes every retry fail with "bind: Address already in use" — kill
            # the exact stale holder before the next attempt. Our own child has
            # already exited by this point, so the pattern can't match it.
            if "Address already in use" in err or "bind:" in err:
                subprocess.run(
                    ["pkill", "-f", f"ssh -N -T.*127.0.0.1:{lport}:"],
                    capture_output=True, timeout=5)
        except Exception as e:
            _set_tunnel(name, False, str(e))
        finally:
            ctl["proc"] = None
        # Only an attempt that clearly outlived connect+bind counts as "was
        # established" and resets the backoff; quick failures (connect timeout,
        # bind refusal) keep doubling so flaky hosts aren't hammered.
        if time.time() - started > 20:
            backoff = 3
        if ctl["run"].is_set() and not _shutting_down:
            # Backoff, but wake early if the host gets paused meanwhile.
            slept = 0.0
            while slept < backoff and ctl["run"].is_set():
                time.sleep(0.5)
                slept += 0.5
            backoff = min(backoff * 2, 60)


def set_tunnel_enabled(name, enabled):
    """Flip a host's tunnel on/off: persist the choice, then wake or park the
    worker (terminating the live ssh child on pause)."""
    update_cockpit_ui({"tunnel": {"name": name, "enabled": bool(enabled)}})
    ctl = _tunnel_ctl.get(name)
    if not ctl:
        return {"ok": False, "error": "unknown host"}
    if enabled:
        ctl["run"].set()
    else:
        ctl["run"].clear()
        proc = ctl.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    return {"ok": True, "name": name, "enabled": bool(enabled)}


_shutting_down = False


def _kill_tunnel_children(*_args):
    """Shut tunnels down for good: park every worker FIRST (so none can spawn
    a fresh ssh between our cleanup and process death — that's how orphans
    were born), then terminate the live children."""
    global _shutting_down
    _shutting_down = True
    for ctl in _tunnel_ctl.values():
        ctl["run"].clear()
    for ctl in _tunnel_ctl.values():
        proc = ctl.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def _sigterm(_sig, _frame):
    _kill_tunnel_children()
    sys.exit(0)   # runs atexit handlers too (terminate twice is harmless)


def start_cockpit_tunnels():
    ui = load_cockpit_ui()
    for host in cockpit_hosts():
        if not host.get("local_port"):
            continue
        name = host.get("name", host["ssh"])
        ev = threading.Event()
        if tunnel_enabled(ui, name):
            ev.set()
        _tunnel_ctl[name] = {"run": ev, "proc": None}
        threading.Thread(target=_tunnel_loop, args=(host,), daemon=True).start()
    atexit.register(_kill_tunnel_children)
    signal.signal(signal.SIGTERM, _sigterm)


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


def _identity_overrides(ui):
    """[(dir, name, color)] longest-first, from user identity overrides."""
    items = []
    for cwd, v in (ui.get("identity") or {}).items():
        items.append((cwd.rstrip("/"), v.get("name", ""), v.get("color", "")))
    items.sort(key=lambda t: len(t[0]), reverse=True)
    return items


def apply_identity(sess, overrides):
    """User overrides win over anything derived — name and/or color."""
    cwd = (sess.get("cwd") or "").rstrip("/")
    for norm, name, color in overrides:
        if norm and (cwd == norm or cwd.startswith(norm + "/")):
            if name:
                sess["window_name"] = name
            if color:
                sess["window_color"] = color
            break
    return sess


def enrich(sess, overrides):
    """Give a session its window name/color by matching cwd to the launcher
    config, unless it already carried explicit identity from its shell; then
    apply any user override on top."""
    cwd = (sess.get("cwd") or "").rstrip("/")
    for norm, name, color in _launcher_dirs():
        if norm and (cwd == norm or cwd.startswith(norm + "/")):
            # The launcher-config name is what launched windows are stamped
            # with (--title=<name>) — keep it separately for click-to-focus,
            # because the display name may be a user identity override.
            if name:
                sess["launch_title"] = name
            if not sess.get("window_name"):
                sess["window_name"] = name
            if not sess.get("window_color"):
                sess["window_color"] = color
            break
    return apply_identity(sess, overrides)


def cockpit_live():
    """Merge local + every configured remote collector into one machine list."""
    ui = load_cockpit_ui()
    overrides = _identity_overrides(ui)
    labels = ui.get("labels", {})
    machines = []
    local = fetch_collector(LOCAL_COLLECTOR)
    machines.append({
        "name": local.get("machine", "mac") if local else "mac",
        "label": "this mac",
        "reachable": local is not None,
        "error": "" if local else "local collector down",
        "local": True,
        "paused": False,
        "sessions": [enrich(s, overrides) for s in local.get("sessions", [])] if local else [],
    })
    for host in cockpit_hosts():
        name = host.get("name", host["ssh"])
        paused = _tunnel_paused(name)
        if paused:
            # Deliberately offline — not an error, no session cards, but the
            # section stays listed so the user can toggle it back on.
            machines.append({
                "name": name, "label": host["ssh"], "host": name,
                "reachable": False, "error": "", "local": False,
                "paused": True, "sessions": [],
            })
            continue
        with _tstate_lock:
            ts = dict(_tunnel_state.get(name, {}))
        data = fetch_collector(host["local_port"]) if ts.get("up") else None
        # Remote sessions get user overrides but not the Mac launcher config.
        sessions = [apply_identity(s, overrides) for s in data.get("sessions", [])] if data else []
        machines.append({
            "name": data.get("machine", name) if data else name,
            "label": host["ssh"],
            "host": name,
            "reachable": data is not None,
            "error": "" if data else (ts.get("error") or "tunnel down"),
            "local": False,
            "paused": False,
            "sessions": sessions,
        })

    # Overlay user-set custom note labels; hand the manual order to the client.
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


def launch_ghostty(directory, background, foreground="#ffffff", ssh=None, title=None):
    directory = os.path.expanduser(directory)
    ghostty_bin = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
    cmd = [
        ghostty_bin,
        f"--background={background}",
        f"--foreground={foreground}",
    ]
    if title:
        # `title` is a Ghostty config key: it both sets the window title AND locks
        # it against shell/OSC overrides, so the Cockpit can focus the window by
        # this deterministic name (project name) later via Hammerspoon.
        cmd.append(f"--title={title}")
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


# Hammerspoon runs a tiny focus server on 127.0.0.1:8460 (see
# deploy/hammerspoon-cockpit.lua). We proxy to it so the browser can raise a
# Ghostty window by its (project-name) title without any cross-origin fuss.
HS_FOCUS_PORT = int(os.environ.get("AGENT_HS_FOCUS_PORT", "8460"))


def transcript_hint(cwd, sid):
    """Freshest AI task summary for a session, read straight from its Claude
    Code transcript (~/.claude/projects/<munged-cwd>/<sid>.jsonl). Claude Code
    retitles unlocked windows with exactly this text, so it's the strongest
    focus needle — and unlike the hook-delivered hint it can't go stale on a
    quiet session. Fully defensive; returns '' on any problem."""
    try:
        sid = re.sub(r"[^A-Za-z0-9-]", "", sid or "")
        cwd = (cwd or "").rstrip("/")
        if not sid or not cwd:
            return ""
        proj = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
        path = os.path.expanduser(f"~/.claude/projects/{proj}/{sid}.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 131072))
            tail = f.read().decode("utf-8", "replace")
        hint = ""
        for line in tail.splitlines():
            if '"ai-title"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                hint = " ".join(str(obj["aiTitle"]).split())[:120]
        return hint
    except Exception:
        return ""


def dismiss_session(sid):
    """Drop a session from the collector rosters so its card disappears. Uses
    the collectors' existing state:"ended" ingest path; broadcast to the local
    collector and every tunnel port — popping an unknown sid is a no-op and
    session ids are unique, so this is safe. If the session is actually alive,
    its next hook event re-ingests it and the card respawns (by design)."""
    if not sid:
        return {"ok": False, "error": "no sid"}
    ports = [LOCAL_COLLECTOR] + [
        int(h["local_port"]) for h in cockpit_hosts()
        if h.get("local_port") and not _tunnel_paused(h.get("name", h["ssh"]))]
    ok = False
    for port in ports:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ingest",
                data=json.dumps({"session_id": sid, "state": "ended"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=1.0).read()
            ok = True
        except Exception:
            pass
    return {"ok": ok}


def focus_window(title, alt="", hint="", sid="", cwd=""):
    """Ask Hammerspoon to focus the Ghostty window matching one of the needles,
    tried in order: `title` (launcher-stamped --title), `alt` (cwd basename),
    `hint` (Claude Code's AI task summary — its live window-retitle text, for
    windows not launched from the Launcher). When sid+cwd are given, the hint
    is re-read fresh from the session transcript at focus time, since the
    hook-delivered hint goes stale on quiet sessions. Always returns JSON; a
    missing/broken Hammerspoon → {ok: false}."""
    fresh = transcript_hint(cwd, sid)
    hint = fresh or hint
    if not (title or alt or hint):
        return {"ok": False, "error": "no title"}
    try:
        url = (f"http://127.0.0.1:{HS_FOCUS_PORT}/focus"
               f"?title={quote(title or '')}&alt={quote(alt or '')}&hint={quote(hint or '')}")
        with urllib.request.urlopen(url, timeout=1.0) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
            min-height: 100vh;
            color: #e2e8f0;
            padding: 2rem;
            /* Synthwave: near-black base with magenta / violet / cyan neon pools. */
            background:
                radial-gradient(1000px 560px at 12% -8%, rgba(255,45,149,.16), transparent 60%),
                radial-gradient(1100px 620px at 92% -4%, rgba(130,50,235,.20), transparent 60%),
                radial-gradient(900px 700px at 78% 112%, rgba(0,225,255,.10), transparent 58%),
                linear-gradient(180deg, #06040c 0%, #0a0616 52%, #06040c 100%);
            background-attachment: fixed;
        }
        /* Synthwave perspective grid receding to a glowing horizon near the
           bottom of the viewport. Fixed + purely CSS so the 1.5s re-render
           never touches it. */
        body::before {
            content:''; position:fixed; left:0; right:0; bottom:0; height:42vh; z-index:-2;
            pointer-events:none;
            background-image:
                linear-gradient(rgba(255,45,149,.40) 1px, transparent 1px),
                linear-gradient(90deg, rgba(0,225,255,.30) 1px, transparent 1px);
            background-size: 100% 34px, 3.2vw 100%;
            transform: perspective(320px) rotateX(64deg);
            transform-origin: bottom center;
            mask-image: linear-gradient(to top, #000 0%, rgba(0,0,0,.5) 45%, transparent 100%);
            -webkit-mask-image: linear-gradient(to top, #000 0%, rgba(0,0,0,.5) 45%, transparent 100%);
            opacity:.55;
        }
        /* Very faint CRT scanlines, fixed over the background. */
        body::after {
            content:''; position:fixed; inset:0; z-index:-1; pointer-events:none;
            background: repeating-linear-gradient(to bottom,
                rgba(255,255,255,.014) 0 1px, transparent 1px 3px);
        }

        /* Full-bleed: span the viewport (body's 2rem padding is the margin) so
           wide windows fit more cards per row; the auto-fill grid wraps rows. */
        .container { width: 100%; margin: 0 auto; position: relative; }

        /* No title text — just the tab strip, right-aligned, above the views. */
        header {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            margin-bottom: 1.25rem;
        }

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
        .tab.active { background: linear-gradient(135deg,#ff2d95,#8b5cf6); color: white;
                      box-shadow: 0 0 18px -3px rgba(255,45,149,.55); }

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
        /* Machine strip: the single, always-visible line of machine state —
           health dot + name (+ ssh toggle for remotes). Sessions themselves
           all flow into one unified grid below. */
        .mstrip { display:flex; flex-wrap:wrap; align-items:center;
                  gap:.5rem 1.4rem; margin-bottom:1rem; min-height:1.4rem; }
        .mstrip .ms { display:inline-flex; align-items:center; gap:.5rem;
                      font-size:.8rem; font-weight:600; text-transform:uppercase;
                      letter-spacing:.06em; color:#94a3b8; }
        .mstrip .ms.ms-paused { opacity:.55; }
        .mstrip .msdot { width:.55rem; height:.55rem; border-radius:50%; flex:none; }
        /* Tunnel on/off switch (opt-in ssh — the primary way a host comes
           online): always visible, clearly labelled, color-coded. */
        .mstrip .tglbtn { cursor:pointer; user-select:none; -webkit-user-select:none;
                          font-size:.68rem; font-weight:700; letter-spacing:.04em;
                          text-transform:uppercase;
                          padding:.18rem .6rem; border-radius:999px;
                          transition:color .12s, border-color .12s, background .12s,
                                     box-shadow .12s; }
        .mstrip .tglbtn.live { color:#8fd4a5; border:1px solid rgba(111,158,128,.6);
                               background:rgba(111,158,128,.14);
                               box-shadow:0 0 10px -4px rgba(111,158,128,.7); }
        .mstrip .tglbtn.live:hover { color:#b9ecc8; border-color:rgba(111,158,128,.9);
                                     background:rgba(111,158,128,.22); }
        .mstrip .tglbtn.off { color:#94a0ac; border:1px solid rgba(148,163,184,.45);
                              background:rgba(255,255,255,.06); }
        .mstrip .tglbtn.off:hover { color:#e2e8f0; border-color:rgba(148,163,184,.8);
                                    background:rgba(255,255,255,.12); }
        /* Left-aligned, wrapping grid: bounded track max keeps cards sane on
           huge windows; sections with few cards stay flush left under their
           header instead of floating in the middle. */
        .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(275px,420px));
                gap:.85rem; justify-content:start; }
        /* Desaturated full outline conveys state; no bright dots. Elevated
           surface so cards float above the cockpit background. */
        .scard { background:linear-gradient(180deg, rgba(40,34,62,.86), rgba(19,15,32,.9));
                 border:1.5px solid rgba(185,165,255,0.16);
                 border-radius:12px; padding:.85rem 1rem .9rem; position:relative; overflow:hidden;
                 min-height:325px; display:flex; flex-direction:column;
                 box-shadow:0 10px 34px -12px rgba(0,0,0,.85), 0 0 0 1px rgba(0,0,0,.35),
                            0 0 26px -16px rgba(180,80,255,.55);
                 backdrop-filter:blur(4px);
                 cursor:grab; transition:border-color .15s, box-shadow .15s, opacity .15s; }
        .scard:active { cursor:grabbing; }
        /* Focusable (mac-local) cards read as clickable; remote cards keep the
           plain grab/drag affordance. */
        .scard.focusable { cursor:pointer; }
        .scard.focusable:active { cursor:grabbing; }
        .scard.dragging { opacity:.4; }
        .scard.dragover { box-shadow:0 0 0 2px rgba(148,163,184,.5); }
        .scard .st { display:flex; align-items:center; gap:.5rem; margin-bottom:.45rem; }
        .scard .sdot { width:.55rem; height:.55rem; border-radius:50%; flex:none; }
        .scard .state { font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
        .scard .subs { font-size:.64rem; color:#cbd5e1; background:rgba(255,255,255,0.08);
                       padding:.05rem .4rem; border-radius:999px; }
        .scard .ctx { font-size:.64rem; color:#8b98a5; background:rgba(255,255,255,0.05);
                      padding:.05rem .4rem; border-radius:999px; font-family:ui-monospace,Menlo,monospace; }
        /* High-context nudge: amber past CTX_WARN tokens ("time to /compact"),
           red past CTX_CRIT (context limit territory). */
        .scard .ctx.ctx-warn { color:#fbbf24; background:rgba(251,191,36,.10);
                               box-shadow:0 0 0 1px rgba(251,191,36,.35) inset; }
        .scard .ctx.ctx-crit { color:#f87171; background:rgba(248,113,113,.12);
                               box-shadow:0 0 0 1px rgba(248,113,113,.45) inset; }
        /* Permission-mode badge — only shown for non-default modes. */
        .scard .pmode { font-size:.62rem; font-weight:700; letter-spacing:.03em;
                        padding:.05rem .4rem; border-radius:999px; flex:none; }
        .scard .pm-plan   { color:#7fd4e0; background:rgba(0,225,255,.10);
                            box-shadow:0 0 0 1px rgba(0,225,255,.25) inset; }
        .scard .pm-auto   { color:#b8a6e8; background:rgba(139,92,246,.14);
                            box-shadow:0 0 0 1px rgba(139,92,246,.30) inset; }
        .scard .pm-bypass { color:#ffb35c; background:rgba(255,90,60,.16);
                            box-shadow:0 0 0 1px rgba(255,120,60,.45) inset; }
        .scard .age { margin-left:auto; font-size:.68rem; color:#64748b; }
        /* The whole name row is the color band — the window color tints the
           header full-bleed, so each card wears its Ghostty identity. */
        /* No text selection in the header: dbl-click means "edit", and the
           inner controls shouldn't fight the I-beam cursor. */
        .scard .proj { font-size:1.02rem; font-weight:650;
                       display:flex; align-items:center; gap:.42rem;
                       margin:-.85rem -1rem .5rem; padding:.5rem 1rem .45rem;
                       border-bottom:1px solid rgba(255,255,255,0.07);
                       user-select:none; -webkit-user-select:none; }
        .scard.editing .proj { user-select:text; -webkit-user-select:text; }
        .scard .swatch { position:relative; width:.95rem; height:.95rem; border-radius:3px;
                         flex:none; box-shadow:0 0 0 1px rgba(255,255,255,0.35) inset;
                         cursor:pointer; overflow:hidden;
                         background-image:linear-gradient(135deg,#888,#bbb); }
        .scard .swatch input { position:absolute; inset:-4px; opacity:0; cursor:pointer;
                               border:none; padding:0; background:none; }
        .scard .editonly { display:none; }
        .scard.editing .editonly { display:inline-block; }
        .scard .editbtn { background:none; border:none; color:#5b6773; cursor:pointer;
                          user-select:none; -webkit-user-select:none;
                          font-size:.78rem; line-height:1; padding:.1rem .2rem; border-radius:4px;
                          opacity:0; transition:opacity .12s, color .12s; }
        .scard:hover .editbtn, .scard.editing .editbtn { opacity:1; }
        .scard .editbtn:hover { color:#cbd5e1; background:rgba(255,255,255,0.06); }
        .scard.editing .editbtn { color:#7faa8f; opacity:1; }
        /* Focus-window button (Mac-local sessions only), same treatment as ✎. */
        .scard .focusbtn { background:none; border:none; color:#5b6773; cursor:pointer;
                           user-select:none; -webkit-user-select:none;
                           font-size:.82rem; line-height:1; padding:.1rem .2rem; border-radius:4px;
                           opacity:0; transition:opacity .12s, color .12s; }
        .scard:hover .focusbtn, .scard.editing .focusbtn { opacity:1; }
        .scard .focusbtn:hover { color:#7fd4e0; background:rgba(0,225,255,0.08); }
        /* Dismiss button — soft-removes the card (respawns on next activity). */
        .scard .xbtn { background:none; border:none; color:#5b6773; cursor:pointer;
                       user-select:none; -webkit-user-select:none;
                       font-size:.78rem; line-height:1; padding:.1rem .2rem; border-radius:4px;
                       opacity:0; transition:opacity .12s, color .12s; }
        .scard:hover .xbtn { opacity:1; }
        .scard .xbtn:hover { color:#e08791; background:rgba(176,110,124,0.12); }
        .scard.editing .xbtn { display:none; }  /* keep it out of edit mode */
        .scard .pname { outline:none; border-radius:5px; padding:.05rem .25rem;
                        margin:-.05rem -.15rem; cursor:default; }
        .scard.editing .pname { cursor:text; background:rgba(255,255,255,0.09);
                                box-shadow:0 0 0 1px rgba(148,163,184,.4); }
        .scard .idtag { font-size:.58rem; color:#5b6773; font-family:ui-monospace,Menlo,monospace; }
        /* Which machine this session lives on — subtle chip beside the id tag.
           Mac stays dim; remotes get a cyan tint so they pop at a glance. */
        .scard .mtag { font-size:.58rem; font-family:ui-monospace,Menlo,monospace;
                       padding:.02rem .38rem; border-radius:999px; flex:none;
                       margin-left:auto; }
        .scard .mtag-local { color:#6b7684; background:rgba(255,255,255,.05); }
        .scard .mtag-remote { color:#7fd4e0; background:rgba(0,225,255,.08);
                              box-shadow:0 0 0 1px rgba(0,225,255,.20) inset; }
        /* Sublabel: plain text in normal mode (empty → hidden, no focus), an
           editable field only inside edit mode. */
        .scard .title { font-size:.8rem; color:#e2e8f0; opacity:.9; margin-bottom:.3rem;
                        border-radius:5px; padding:.1rem .25rem; margin-left:-.25rem;
                        outline:none; cursor:default; }
        .scard .title:empty { display:none; }
        /* Explicit :empty variant so the hide rule can never out-cascade this;
           an unmistakable dashed field so the note is obviously writable. */
        .scard.editing .title, .scard.editing .title:empty {
            display:block; cursor:text; min-height:1.6em;
            background:rgba(255,255,255,0.07);
            box-shadow:0 0 0 1px rgba(148,163,184,.45);
            border:1px dashed rgba(148,163,184,.35); }
        .scard.editing .title:empty::before { content:'add note…'; color:#8b98a5; font-style:italic; }
        .scard.editing .title:hover { background:rgba(255,255,255,0.10); }
        .scard.editing .title:focus { background:rgba(255,255,255,0.10); border-style:solid;
                              box-shadow:0 0 0 1px rgba(148,163,184,.6); }
        .scard .lastprompt { font-size:.72rem; color:#93a3b5; line-height:1.4; margin-bottom:.4rem;
                             padding-left:.5rem; border-left:2px solid rgba(148,163,184,.28);
                             display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical;
                             overflow:hidden; }
        .scard .lastprompt::before { content:'you: '; color:#5b6773; }
        .scard .activity { font-size:.75rem; color:#9aa7b4; line-height:1.42; margin-bottom:.4rem;
                           display:-webkit-box; -webkit-line-clamp:10; -webkit-box-orient:vertical;
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
                    ssh: p.ssh || null,
                    title: p.name || null
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
                body: JSON.stringify({ directory: c.pwd || '~', background: '#16213e', foreground: '#ffffff', title: c.project || null })
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
        // Context-size nudge thresholds (tokens): amber = time to /compact,
        // red = context limit territory.
        const CTX_WARN = 150000, CTX_CRIT = 185000;
        let cockpitTimer = null;
        let manualOrder = [];   // session_id[] — user's drag order
        let cpBusy = false;     // editing a label or dragging → pause re-render

        function agoSec(a){ if(a==null) return ''; a=Math.max(0,a|0);
          if(a<60) return a+'s'; if(a<3600) return (a/60|0)+'m'; return (a/3600|0)+'h'; }
        function fmtTok(n){ if(!n) return '';
          return n>=1000 ? (n/1000).toFixed(n>=100000?0:1).replace(/\.0$/,'')+'k' : ''+n; }
        function shortModel(m){ return (m||'').replace(/^claude-/,'').replace(/-\d{6,}$/,''); }
        function hexA(hex, a){ hex=(hex||'').replace('#','');
          if(hex.length===3) hex=hex.split('').map(c=>c+c).join('');
          if(hex.length!==6) return `rgba(136,136,136,${a})`;
          const n=parseInt(hex,16);
          return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`; }
        function orderIndex(sid){ const i = manualOrder.indexOf(sid); return i<0 ? 1e6 : i; }

        async function saveUI(patch){
          try {
            const r = await fetch('/api/ui', {method:'POST',
              headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
            const ui = await r.json();
            if(ui && ui.order) manualOrder = ui.order;
          } catch(e){}
        }

        const PMODES = {plan:{t:'plan',c:'pm-plan'},
                        acceptEdits:{t:'auto-edit',c:'pm-auto'},
                        bypassPermissions:{t:'BYPASS',c:'pm-bypass'}};

        // Everything a card renders, precomputed once per session per tick so
        // the full-rebuild and in-place-patch paths share identical values.
        function cardBits(s, isLocal){
          const ui = CUI[s.state] || {l:s.state, c:'stale'};
          const cwd = s.cwd || '';
          const color = s.window_color || '#888888';
          const focusTitle = (s.launch_title || s.window_name || '').trim();
          const cwdBase = (cwd.replace(/\/+$/,'').split('/').pop() || '').trim();
          const focusHint = (s.title_hint || '').trim();
          const canFocus = !!((s.machine==='mac') && (focusTitle || cwdBase || focusHint));
          const pmInfo = PMODES[s.permission_mode];
          const ctxCls = s.context_tokens>=CTX_CRIT ? ' ctx-crit'
                       : s.context_tokens>=CTX_WARN ? ' ctx-warn' : '';
          const marker = s.state==='working'
            ? '<span class="work-pip"></span>' : '<span class="sdot"></span>';
          const stCore = marker
            + `<span class="state">${escapeHtml(ui.l)}</span>`
            + (pmInfo ? `<span class="pmode ${pmInfo.c}" title="permission mode: ${escapeHtml(s.permission_mode)}">${pmInfo.t}</span>` : '')
            + ((s.subagents>0) ? `<span class="subs">▷ ${s.subagents} sub${s.subagents>1?'s':''}</span>` : '')
            + (s.context_tokens ? `<span class="ctx${ctxCls}" title="context tokens / model${ctxCls?' — time to /compact':''}">${fmtTok(s.context_tokens)}${s.model?(' · '+escapeHtml(shortModel(s.model))):''}</span>` : '');
          return {
            sid: s.session_id || '',
            cls: `scard s-${ui.c}${canFocus?' focusable':''}`,
            canFocus,
            ord: CORDER[s.state] ?? 9,
            mtag: s.machine || '?',
            mtagCls: isLocal ? 'mtag-local' : 'mtag-remote',
            ft: focusTitle, fa: cwdBase, fh: focusHint,
            name: s.window_name || s.project || '?',
            cwd, color,
            headStyle: `background:linear-gradient(100deg, ${hexA(color,.42)} 0%, ${hexA(color,.14)} 55%, ${hexA(color,.02)} 100%);`,
            stCore,
            age: agoSec(s.age),
            label: s.custom_title || s.title || '',
            lastp: s.last_prompt || '',
            activity: s.activity || '',
            detail: s.detail || '',
          };
        }

        // Fingerprint of the last full render, and the bits it applied, so the
        // 1.5s poll can patch in place instead of nuking innerHTML — rebuilding
        // destroys the node under the cursor and makes it flicker arrow<->hand.
        let cockpitRoster = '';
        let appliedBits = {};   // sid -> bits last written to the DOM

        function patchCard(card, b){
          // A tick fetched before enterEdit can land mid-edit (cpBusy only
          // gates the start of a tick) — never touch a card being edited.
          if(card.classList.contains('editing')) return;
          const prev = appliedBits[b.sid] || {};
          if(prev.cls !== b.cls) card.className = b.cls;
          if(prev.ft !== b.ft) card.dataset.ft = b.ft;
          if(prev.fa !== b.fa) card.dataset.fa = b.fa;
          if(prev.fh !== b.fh) card.dataset.fh = b.fh;
          if(prev.name !== b.name){
            const el = card.querySelector('.pname'); if(el) el.textContent = b.name;
          }
          if(prev.headStyle !== b.headStyle){
            const el = card.querySelector('.proj'); if(el) el.setAttribute('style', b.headStyle);
          }
          if(prev.color !== b.color){
            const el = card.querySelector('.swatchpick'); if(el) el.value = b.color;
          }
          const st = card.querySelector('.st');
          if(st){
            if(prev.stCore !== b.stCore){
              st.innerHTML = b.stCore + `<span class="age">${escapeHtml(b.age)}</span>`;
            } else if(prev.age !== b.age){
              const el = st.querySelector('.age'); if(el) el.textContent = b.age;
            }
          }
          if(prev.label !== b.label){
            const el = card.querySelector('.title'); if(el) el.textContent = b.label;
          }
          if(prev.lastp !== b.lastp){
            const el = card.querySelector('.lastprompt'); if(el) el.textContent = b.lastp;
          }
          if(prev.activity !== b.activity){
            const el = card.querySelector('.activity'); if(el) el.textContent = b.activity;
          }
          if(prev.detail !== b.detail){
            const el = card.querySelector('.detail');
            if(el){ el.textContent = b.detail || ' '; el.title = b.detail; }
          }
          if(prev.cwd !== b.cwd){
            const el = card.querySelector('.cwd'); if(el) el.textContent = b.cwd;
          }
          appliedBits[b.sid] = b;
        }

        function cardHTML(b){
          const colorinp = `<label class="swatch editonly" title="recolor" onclick="event.stopPropagation()"><input type="color" class="swatchpick" data-cwd="${escapeHtml(b.cwd)}" value="${escapeHtml(b.color)}"></label>`;
          const editbtn = `<button class="editbtn" title="edit name & color" onclick="event.stopPropagation()">✎</button>`;
          const focusbtn = b.canFocus
            ? `<button class="focusbtn" title="focus Ghostty window" onclick="event.stopPropagation()">⤢</button>` : '';
          const focusData = b.canFocus
            ? ` data-ft="${escapeHtml(b.ft)}" data-fa="${escapeHtml(b.fa)}" data-fh="${escapeHtml(b.fh)}"` : '';
          const lastp = b.lastp
            ? `<div class="lastprompt" title="your latest prompt">${escapeHtml(b.lastp)}</div>` : '';
          return `<div class="${b.cls}" draggable="true" data-sid="${escapeHtml(b.sid)}"${focusData}>
            <div class="proj" style="${b.headStyle}"><span class="pname" contenteditable="false" spellcheck="false" data-cwd="${escapeHtml(b.cwd)}">${escapeHtml(b.name)}</span>${colorinp}${editbtn}${focusbtn}<span class="mtag ${b.mtagCls}" title="machine: ${escapeHtml(b.mtag)}">${escapeHtml(b.mtag)}</span><span class="idtag">${escapeHtml(b.sid.slice(0,6))}</span><button class="xbtn" title="dismiss (respawns on next activity)" onclick="event.stopPropagation()">✕</button></div>
            <div class="st">${b.stCore}<span class="age">${escapeHtml(b.age)}</span></div>
            <div class="title" contenteditable="false" spellcheck="false" data-sid="${escapeHtml(b.sid)}">${escapeHtml(b.label)}</div>
            ${lastp}
            <div class="activity">${escapeHtml(b.activity)}</div>
            <div class="foot">
              <div class="detail" title="${escapeHtml(b.detail)}">${escapeHtml(b.detail)||'&nbsp;'}</div>
              <div class="cwd">${escapeHtml(b.cwd)}</div>
            </div>
          </div>`;
        }

        async function cockpitTick(){
          if(cpBusy) return;   // never clobber an in-progress edit / drag
          let data;
          try { data = await (await fetch('/api/live')).json(); }
          catch(e){ return; }
          const machines = data.machines || [];
          manualOrder = data.order || [];
          let need=0, work=0;
          // One unified card list across all machines (paused/unreachable
          // machines contribute nothing — their state lives in the strip).
          const allBits = [];
          for(const m of machines){
            for(const s of (m.sessions||[])){
              if(s.state==='needs_input') need++;
              if(s.state==='working') work++;
              allBits.push(cardBits(s, !!m.local));
            }
          }
          // Manual drag order first, then state priority.
          allBits.sort((a,b)=>{
            const oa=orderIndex(a.sid), ob=orderIndex(b.sid);
            return oa!==ob ? oa-ob : a.ord-b.ord;
          });
          const total = allBits.length;
          // Structural fingerprint: machine strip state (health/pause/errors)
          // + card list/order + per-card structure. While it's unchanged we
          // patch nodes in place; a rebuild only happens when the layout (or
          // the strip) actually changes.
          const roster = JSON.stringify([
            machines.map(m=>[m.name, m.label, m.reachable, m.error, !!m.paused, !!m.local]),
            allBits.map(b=>[b.sid, b.canFocus, !!b.lastp])
          ]);
          const container = document.getElementById('machines');
          if(roster === cockpitRoster && container.querySelector('.mstrip')){
            for(const b of allBits){
              const card = container.querySelector(`.scard[data-sid="${b.sid}"]`);
              if(card) patchCard(card, b);
            }
          } else {
            // Machine strip: dot + name (+ ssh toggle for remotes). Green =
            // reachable, dim gray = paused (deliberate), red = enabled but
            // unreachable (error in the tooltip).
            let strip = '<div class="mstrip">';
            for(const m of machines){
              const dot = m.paused ? '#5a6472' : (m.reachable ? '#6f9e80' : '#b06e7c');
              const stateTip = m.paused ? 'tunnel paused'
                             : (m.reachable ? 'connected' : (m.error || 'unreachable'));
              const tgl = m.local ? '' : (m.paused
                ? `<button class="tglbtn off" data-host="${escapeHtml(m.host||m.name)}" data-on="0" title="no ssh connection — click to connect">ssh&nbsp;⏸&nbsp;off</button>`
                : `<button class="tglbtn live" data-host="${escapeHtml(m.host||m.name)}" data-on="1" title="ssh tunnel live — click to disconnect">ssh&nbsp;▶&nbsp;live</button>`);
              strip += `<span class="ms${m.paused?' ms-paused':''}" title="${escapeHtml(m.label||'')} — ${escapeHtml(stateTip)}">
                <span class="msdot" style="background:${dot}"></span>${escapeHtml(m.name)}${tgl}</span>`;
            }
            strip += '</div>';
            container.innerHTML = strip + (total
              ? '<div class="grid">' + allBits.map(cardHTML).join('') + '</div>'
              : '<div class="none">No sessions reporting yet.<br>Start a Claude session on any wired machine.</div>');
            wireCockpitCards();
            cockpitRoster = roster;
            appliedBits = {};
            for(const b of allBits) appliedBits[b.sid] = b;
          }
          const bits = [];
          if(need) bits.push(need+' need you');
          if(work) bits.push(work+' working');
          bits.push(total+' total');
          document.getElementById('cockpit-sub').textContent =
            bits.join(' · ') + ' · ' + new Date().toLocaleTimeString();
          document.title = (need ? `(${need}!) ` : '') + 'Ghostty Launcher';
        }

        // Click-outside = accept: while a card is editing, a mousedown anywhere
        // outside it commits the edit exactly like ✓. Registered on capture so
        // it runs before other handlers; the trailing click is swallowed via
        // justDragged so it can't double as a focus request on another card.
        let editingCard = null;
        function onDocMousedown(e){
          const card = editingCard;
          if(!card) return;
          if(e.target && e.target.closest && e.target.closest('.scard') === card) return;
          exitEdit(card);
          justDragged = true; setTimeout(()=>{ justDragged = false; }, 250);
        }

        function enterEdit(card){
          const pname = card.querySelector('.pname');
          const title = card.querySelector('.title');
          const btn = card.querySelector('.editbtn');
          cpBusy = true; card.draggable = false; card.classList.add('editing');
          if(btn) btn.textContent = '✓';
          if(title){
            title.contentEditable = 'true';   // sublabel editable in edit mode
            // contenteditable often leaves a stray <br>/whitespace after the
            // user deletes text — normalize so :empty (placeholder) works.
            if(!title.textContent.trim()) title.innerHTML = '';
            // Remember initial values so exit only saves actual changes —
            // otherwise the fallback conversation title gets silently saved
            // as a custom label just by opening and closing edit mode.
            card.dataset.label0 = title.textContent.trim();
          }
          if(pname){
            pname.contentEditable = 'true';
            card.dataset.name0 = pname.textContent.trim();
            pname.focus();
            const r = document.createRange(); r.selectNodeContents(pname); r.collapse(false);
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
          }
          editingCard = card;
          // The mousedown that produced the ✎/dblclick already happened, so
          // registering now can't self-trigger.
          document.addEventListener('mousedown', onDocMousedown, true);
        }
        function exitEdit(card){
          document.removeEventListener('mousedown', onDocMousedown, true);
          editingCard = null;
          const pname = card.querySelector('.pname');
          const title = card.querySelector('.title');
          const btn = card.querySelector('.editbtn');
          if(pname){
            pname.contentEditable = 'false';
            const name = pname.textContent.trim();
            if(name !== card.dataset.name0)
              saveUI({identity: true, cwd: pname.dataset.cwd, name});
          }
          if(title){
            title.contentEditable = 'false';
            const label = title.textContent.trim();
            if(!label) title.innerHTML = '';   // shed stray <br> so :empty hides it
            if(label !== card.dataset.label0)
              saveUI({session_id: title.dataset.sid, label});
          }
          if(btn) btn.textContent = '✎';
          card.classList.remove('editing'); card.draggable = true; cpBusy = false;
        }

        function persistOrder(){
          const sids = [...document.querySelectorAll('.scard')].map(c=>c.dataset.sid);
          manualOrder = sids;
          saveUI({order: sids});
        }

        let dragEl = null;
        let justDragged = false;  // suppress the click that trails a drag-reorder

        // Raise the Ghostty window for a card, using its candidate needles
        // (stamped title, cwd basename, AI task summary) in order. sid+cwd let
        // the server re-read the freshest AI summary from the transcript, since
        // the hook-delivered hint goes stale on quiet sessions.
        function sendFocus(card){
          if(!card || !card.classList.contains('focusable')) return;
          const pname = card.querySelector('.pname');
          fetch('/api/focus?title=' + encodeURIComponent(card.dataset.ft || '')
                + '&alt=' + encodeURIComponent(card.dataset.fa || '')
                + '&hint=' + encodeURIComponent(card.dataset.fh || '')
                + '&sid=' + encodeURIComponent(card.dataset.sid || '')
                + '&cwd=' + encodeURIComponent((pname && pname.dataset.cwd) || '')).catch(()=>{});
        }
        function wireCockpitCards(){
          // Sublabel note — editable only inside edit mode; Enter/Escape ends the
          // edit (and saves), consistent with the name field.
          document.querySelectorAll('.scard .title').forEach(el=>{
            el.addEventListener('keydown', e=>{
              if(e.key==='Enter' || e.key==='Escape'){ e.preventDefault(); exitEdit(el.closest('.scard')); }
            });
          });
          // ✎ toggles an edit panel: name + sublabel become editable + colorpicker
          // appears. Edit mode is sticky (clicking the colorpicker won't dismiss
          // it); ✎ / Enter / Escape ends it.
          document.querySelectorAll('.scard .editbtn').forEach(btn=>{
            const card = btn.closest('.scard');
            btn.addEventListener('click', e=>{
              e.stopPropagation();
              card.classList.contains('editing') ? exitEdit(card) : enterEdit(card);
            });
          });
          // Double-click the color header band to enter edit mode. Ignore the
          // inner controls (swatch / ✎ / ⤢) so they keep their own behavior, and
          // do nothing if already editing (lets you select header text then).
          document.querySelectorAll('.scard .proj').forEach(proj=>{
            proj.addEventListener('dblclick', e=>{
              if(e.target.closest('.swatch, .editbtn, .focusbtn, .xbtn')) return;
              const card = proj.closest('.scard');
              if(card.classList.contains('editing')) return;
              enterEdit(card);
            });
          });
          // ⤢ focuses the matching Ghostty window (Mac-local sessions only).
          document.querySelectorAll('.scard .focusbtn').forEach(btn=>{
            btn.addEventListener('click', e=>{
              e.stopPropagation();
              if(justDragged) return;  // swallowed click (drag / outside-commit)
              sendFocus(btn.closest('.scard'));
            });
          });
          // Tunnel on/off chips on remote machine headers.
          document.querySelectorAll('.mstrip .tglbtn').forEach(btn=>{
            btn.addEventListener('click', async e=>{
              e.stopPropagation();
              const enable = btn.dataset.on !== '1';
              btn.disabled = true;
              try {
                await fetch('/api/tunnel', {method:'POST',
                  headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({name: btn.dataset.host, enabled: enable})});
              } catch(err){}
              cockpitTick();   // re-render with the new paused state now
            });
          });
          // ✕ soft-dismisses the session from its collector; if the session is
          // alive its next hook event re-ingests it and the card respawns.
          document.querySelectorAll('.scard .xbtn').forEach(btn=>{
            btn.addEventListener('click', async e=>{
              e.stopPropagation();
              if(justDragged) return;
              const card = btn.closest('.scard');
              try {
                await fetch('/api/dismiss', {method:'POST',
                  headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({sid: card.dataset.sid})});
              } catch(err){}
              cockpitTick();   // refresh now — don't wait for the next poll
            });
          });
          // Plain click anywhere on a focusable card's background also raises
          // its window. Guards: not in edit mode, not on interactive bits
          // (buttons / swatch / name / note), not the click that trails a
          // drag-reorder, and not a click where the mouse actually moved.
          document.querySelectorAll('.scard.focusable').forEach(card=>{
            card.addEventListener('mousedown', e=>{ card._mx = e.clientX; card._my = e.clientY; });
            card.addEventListener('click', e=>{
              if(justDragged || card.classList.contains('editing')) return;
              if(e.target.closest('.editbtn, .focusbtn, .swatch, .pname, .title, button, input, label')) return;
              if(card._mx != null &&
                 (Math.abs(e.clientX - card._mx) > 5 || Math.abs(e.clientY - card._my) > 5)) return;
              sendFocus(card);
            });
          });
          document.querySelectorAll('.scard .pname').forEach(el=>{
            el.addEventListener('keydown', e=>{
              if(e.key==='Enter' || e.key==='Escape'){ e.preventDefault(); exitEdit(el.closest('.scard')); }
            });
          });
          document.querySelectorAll('.scard .swatchpick').forEach(inp=>{
            inp.addEventListener('input', ()=>{
              const v = inp.value, proj = inp.closest('.proj');
              // Window color lives only in the header band now (no left spine).
              if(proj) proj.style.background = `linear-gradient(100deg, ${hexA(v,.42)} 0%, ${hexA(v,.14)} 55%, ${hexA(v,.02)} 100%)`;
            });
            inp.addEventListener('change', ()=>{ saveUI({identity: true, cwd: inp.dataset.cwd, color: inp.value}); });
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
              // Swallow the click some browsers fire right after a drag so a
              // reorder never doubles as a focus request.
              justDragged = true; setTimeout(()=>{ justDragged = false; }, 250);
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
        elif self.path.startswith("/api/focus"):
            q = parse_qs(urlparse(self.path).query)
            self._send_response(json.dumps(focus_window(q.get("title", [""])[0],
                                                        q.get("alt", [""])[0],
                                                        q.get("hint", [""])[0],
                                                        q.get("sid", [""])[0],
                                                        q.get("cwd", [""])[0])),
                                "application/json")
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
                data.get("ssh"),
                data.get("title")
            )
            self._send_response(json.dumps({"ok": success}), "application/json")

        elif self.path == "/api/ui":
            ui = update_cockpit_ui(json.loads(body))
            self._send_response(json.dumps(ui), "application/json")

        elif self.path == "/api/dismiss":
            data = json.loads(body or "{}")
            self._send_response(json.dumps(dismiss_session(data.get("sid", ""))),
                                "application/json")

        elif self.path == "/api/tunnel":
            data = json.loads(body or "{}")
            self._send_response(
                json.dumps(set_tunnel_enabled(data.get("name", ""),
                                              bool(data.get("enabled", True)))),
                "application/json")

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
