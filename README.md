# Ghostty Launcher

A browser-based dashboard for launching [Ghostty](https://ghostty.org) terminal windows with per-project colors and settings, plus a project **Status** board.

![Python 3](https://img.shields.io/badge/python-3.6+-blue) ![No Dependencies](https://img.shields.io/badge/dependencies-none-green) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## Features

- Launch Ghostty terminals with custom background/foreground colors per project
- SSH session support — launch directly into remote servers
- Add, edit, and delete project entries from the web UI
- Color picker for easy theme customization
- **Status** tab: view per-project status cards (focus / blocker / next), browse
  git-backed history, and open a project in Ghostty. Auto-refreshes every 2 min
  (and on demand via the **↻ Refresh** button).
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
