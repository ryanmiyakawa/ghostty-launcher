# Ghostty Launcher

A browser-based dashboard for launching [Ghostty](https://ghostty.org) terminal windows with per-project colors and settings.

![Python 3](https://img.shields.io/badge/python-3.6+-blue) ![No Dependencies](https://img.shields.io/badge/dependencies-none-green) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## Features

- Launch Ghostty terminals with custom background/foreground colors per project
- SSH session support — launch directly into remote servers
- Add, edit, and delete project entries from the web UI
- Color picker for easy theme customization
- Zero dependencies — pure Python standard library

## Usage

```bash
python3 ghostty_launcher.py
```

This starts a local server on port 8457 and opens the dashboard in your browser.

Click a project card to launch a Ghostty window. Click **+** to add a new project. Hover over a card and click **Edit** to modify or delete it.

## Requirements

- Python 3.6+
- [Ghostty](https://ghostty.org) installed at `/Applications/Ghostty.app` (macOS)
- No pip dependencies

## Configuration

Project entries are saved to `config.json` in the same directory as the script. This file is created automatically when you add your first project through the UI.
