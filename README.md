# Ghostty Launcher + Agent Cockpit

A browser-based dashboard (port 8457) with three tabs:

- **🛩️ Cockpit** — live status of every running Claude Code session across all
  your machines (Mac + SSH-only VPSes): 🟢 working / 🔴 needs you / 🔵 done / ⚪ stale,
  with the conversation name, what Claude last said, subagent count, and each
  session color-matched to its Ghostty window.
- **Launcher** — launch Ghostty terminals with per-project colors / SSH targets.
- **History** — git-backed per-project status board (focus / blocker / next).

![Python 3](https://img.shields.io/badge/python-3.6+-blue) ![No Dependencies](https://img.shields.io/badge/dependencies-none-green)

## The Cockpit — how it works

Live status is driven by **Claude Code hooks**, not polling or git, so it's
reliable and real-time. Three pieces:

| Component | Runs where | Role |
|-----------|-----------|------|
| `emit.py` | every machine (as a hook) | maps each hook event to a session state + enriches it (title, activity, subagents) and POSTs to the local collector |
| `collector.py` | every machine (`127.0.0.1:8458`, localhost-only) | holds that machine's live sessions in memory, serves `GET /status` |
| `ghostty_dashboard.py` | Mac only (`127.0.0.1:8457`) | maintains one SSH tunnel per VPS, merges all collectors, serves the Cockpit UI |

Nothing binds a public port. The only cross-machine traffic is the SSH tunnels
the dashboard owns (the Mac initiates them, so NAT is a non-issue). A dead tunnel
just greys out that machine's card.

### Click-to-focus Ghostty windows (Mac-local)

Cards for Mac-local sessions show a **⤢** button (on hover) that raises the
matching Ghostty window. The plumbing:

- The **Launcher** stamps every window it opens with `--title=<project name>`
  (Ghostty's `title` config key, which also locks the title against shell/OSC
  overrides), so each window has a deterministic, matchable name.
- **Hammerspoon** runs a tiny focus server on `127.0.0.1:8460`
  (`GET /focus?title=<name>` → finds a Ghostty window whose title contains that
  string and `:focus()`es it). Install `deploy/hammerspoon-cockpit.lua` — append
  its `-- >>> agent-cockpit … -- <<< agent-cockpit` block to
  `~/.hammerspoon/init.lua`, then reload Hammerspoon. It needs Hammerspoon's
  **Accessibility** permission to raise windows.
- The dashboard proxies `GET /api/focus?title=…` to that server (short timeout,
  graceful failure), so the browser never talks to Hammerspoon directly.

Only sessions on `machine == "mac"` with a resolved window name get the button;
remote sessions don't. Windows launched *before* a session was titled won't have
a stamped title and won't match — relaunch them from the Launcher.

### Setup

- **This Mac:** `./install-mac.sh` (symlinks + collector launchd service), and make
  sure `deploy/hooks.json`'s `hooks` block is in `~/.claude/settings.json`.
- **A VPS:** clone this repo there, run `./install-remote.sh <label>`, merge the
  hooks block into that machine's `~/.claude/settings.json`, then add the host to
  `~/.claude/agent-cockpit-hosts.json` on the Mac (see `config/hosts.example.json`)
  and restart the dashboard.

Session cards inherit their name + color by matching the working directory against
your Launcher projects (`ghostty_dashboard_config.json`) — one source of truth.

## Launcher / History features

- Launch Ghostty terminals with custom background/foreground colors per project
- SSH session support — launch directly into remote servers
- Add, edit, and delete project entries from the web UI
- **History** tab: per-project status cards (focus / blocker / next) + git history
- Zero dependencies — pure Python standard library

## Usage

```bash
python3 ghostty_dashboard.py
```

This starts a local server on port 8457 and opens the dashboard in your browser.

Click a project card to launch a Ghostty window. Click **+** to add a new project. Hover over a card and click **Edit** to modify or delete it.

### Installed layout

On this machine the launcher is wired into `~/.claude` via symlinks that point
back at this repo, so edits here are the live code:

- `~/.claude/ghostty_dashboard.py` → `ghostty_dashboard.py` (the server + UI)
- `~/.claude/ghostty-launcher` → `ghostty-launcher` (wrapper that execs the `.py`)
- `~/.claude/restart-ghostty-launcher.sh` → `restart-ghostty-launcher.sh`

Restart the running service after editing:

```bash
bash ~/.claude/restart-ghostty-launcher.sh
```

## Requirements

- Python 3.6+
- [Ghostty](https://ghostty.org) installed at `/Applications/Ghostty.app` (macOS)
- No pip dependencies

## Configuration

Project entries are saved to `ghostty_dashboard_config.json` (in `~/.claude`).
This file is created automatically when you add your first project through the UI
and is gitignored. The Status board reads from the separate `~/project-status`
repo (`status.json` + `status/*.json`).
