#!/usr/bin/env python3
"""
Storybook — git-history timeline for launcher projects.

Phase 1: pure repo discovery + raw commit timelines derived from each
project's real `git log`. No AI, no network. Every subprocess call is
timeout-guarded and swallows errors so these functions never raise — the
UI simply shows "no history" on failure.

Consumed by ghostty_dashboard.py via:
  - GET /api/storybook/overview  -> repo_overview()
  - GET /api/storybook?project=  -> commit_timeline(name, skip=n)
"""

import json
import os
import subprocess
import time

# Same launcher config the dashboard reads.
CONFIG_PATH = os.path.expanduser("~/.claude/ghostty_dashboard_config.json")

# Field / record separators for compact single-call `git log` parsing.
# %x1f = unit separator (between fields), %x1e = record separator (between commits).
_FS = "\x1f"
_RS = "\x1e"

_GIT_TIMEOUT = 10


def _run_git(args, timeout=_GIT_TIMEOUT):
    """Run a git command, returning CompletedProcess or None on any failure."""
    try:
        return subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


def _relative(epoch):
    """Human relative string from an epoch seconds value ('3h ago', '2d ago')."""
    if not epoch:
        return ""
    try:
        s = time.time() - float(epoch)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        s = 0
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    if s < 604800:
        return f"{int(s // 86400)}d ago"
    if s < 2592000:
        return f"{int(s // 604800)}w ago"
    if s < 31536000:
        return f"{int(s // 2592000)}mo ago"
    return f"{int(s // 31536000)}y ago"


def _load_launcher_config():
    """Return the launcher config array (or [] on any error)."""
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError, FileNotFoundError):
        return []


def discover_repos():
    """
    Resolve launcher projects to their git roots.

    Skips entries with an empty directory or a non-null ssh (remote-only).
    Dedupes by resolved git root; the first launcher entry for a root wins
    for name/icon/colors. Returns a list of
    {name, icon, background, foreground, root}.
    """
    repos = []
    seen = set()
    for entry in _load_launcher_config():
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory") or ""
        if not directory:
            continue  # remote-only / unset — skip in Phase 1
        if entry.get("ssh"):
            continue  # remote host — skip in Phase 1
        path = os.path.expanduser(directory)
        if not os.path.isdir(path):
            continue
        res = _run_git(["-C", path, "rev-parse", "--show-toplevel"])
        if res is None or res.returncode != 0:
            continue  # not a git repo
        root = res.stdout.strip()
        if not root or root in seen:
            continue
        seen.add(root)
        repos.append({
            "name": entry.get("name") or os.path.basename(root),
            "icon": entry.get("icon") or "📁",
            "background": entry.get("background") or "#16213e",
            "foreground": entry.get("foreground") or "#ffffff",
            "root": root,
        })
    return repos


def _resolve_project(project_name):
    """Look up a project by NAME in discover_repos() output (path-traversal safe)."""
    if not project_name:
        return None
    for repo in discover_repos():
        if repo["name"] == project_name:
            return repo
    return None


def repo_overview():
    """
    Cross-project 'where are things' overview.

    For each discovered repo, cheaply fetch its most recent commit and return
    the list sorted by last-commit date DESCENDING (most recently worked-on
    first). Repos with no commits / unreadable get null last-commit fields
    rather than crashing.
    """
    out = []
    for repo in discover_repos():
        item = {
            "name": repo["name"],
            "icon": repo["icon"],
            "background": repo["background"],
            "foreground": repo["foreground"],
            "root": repo["root"],
            "last_sha": None,
            "last_subject": None,
            "last_author": None,
            "last_commit_epoch": None,
            "last_relative": "",
        }
        fmt = _FS.join(["%h", "%s", "%an", "%ct"])
        res = _run_git(["-C", repo["root"], "log", "-1", f"--format={fmt}"])
        if res is not None and res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip("\n").split(_FS)
            if len(parts) == 4:
                try:
                    epoch = int(parts[3])
                except (ValueError, TypeError):
                    epoch = None
                item["last_sha"] = parts[0]
                item["last_subject"] = parts[1]
                item["last_author"] = parts[2]
                item["last_commit_epoch"] = epoch
                item["last_relative"] = _relative(epoch)
        out.append(item)

    out.sort(key=lambda r: r["last_commit_epoch"] or 0, reverse=True)
    return out


def commit_timeline(project_name, limit=100, skip=0):
    """
    Reverse-chronological commit timeline for a named project.

    `project_name` is resolved against discover_repos() (never a client path).
    `limit` caps commits (default 100, bounding eventual AI backfill cost).
    `skip` maps to `git log --skip=N` so the UI can page 'load older'.

    Returns a list of commit dicts; empty list on any error / unknown project.
    Uses a single `git log --numstat` call and parses it (no per-commit forks).
    Phase 1 deliberately excludes the raw diff.
    """
    repo = _resolve_project(project_name)
    if repo is None:
        return []

    try:
        limit = max(1, min(int(limit), 1000))
    except (ValueError, TypeError):
        limit = 100
    try:
        skip = max(0, int(skip))
    except (ValueError, TypeError):
        skip = 0

    # Header line per commit: full sha, short sha, author, email, author epoch,
    # committer epoch, subject, body. Fields joined by _FS, records by _RS.
    # Followed by numstat lines (one per file) until the next record separator.
    fmt = _RS + _FS.join(["%H", "%h", "%an", "%ae", "%at", "%ct", "%s", "%b"])
    res = _run_git([
        "-C", repo["root"], "log",
        f"--skip={skip}", f"--max-count={limit}",
        "--numstat", f"--format={fmt}",
    ])
    if res is None or res.returncode != 0 or not res.stdout.strip():
        return []

    commits = []
    # Records are separated by _RS. The first chunk before the first _RS is empty.
    for record in res.stdout.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        # Split header from the numstat block: header is line(s) up to the first
        # newline that is NOT inside the body. Since %b (body) may contain
        # newlines, we split on _FS first: the last field (body+numstat) needs
        # care. Simpler: the header has exactly 8 _FS-joined fields; numstat
        # lines follow after a blank separation. Because %b can contain newlines
        # and the numstat lines come right after, we locate numstat by the
        # trailing tab-delimited numeric lines.
        fields = record.split(_FS)
        if len(fields) < 8:
            continue
        full_sha = fields[0]
        short_sha = fields[1]
        author_name = fields[2]
        author_email = fields[3]
        try:
            author_epoch = int(fields[4])
        except (ValueError, TypeError):
            author_epoch = None
        try:
            committer_epoch = int(fields[5])
        except (ValueError, TypeError):
            committer_epoch = None
        subject = fields[6]
        # fields[7] is body + numstat block glued together (numstat has no _FS).
        tail = fields[7]

        body_lines = []
        files_changed = 0
        insertions = 0
        deletions = 0
        for line in tail.split("\n"):
            stripped = line.rstrip("\r")
            # numstat line: "<added>\t<deleted>\t<path>" (added/deleted may be '-').
            cols = stripped.split("\t")
            if len(cols) == 3 and (cols[0].isdigit() or cols[0] == "-") \
                    and (cols[1].isdigit() or cols[1] == "-"):
                files_changed += 1
                if cols[0].isdigit():
                    insertions += int(cols[0])
                if cols[1].isdigit():
                    deletions += int(cols[1])
            else:
                body_lines.append(stripped)

        body = "\n".join(body_lines).strip()

        iso = ""
        if author_epoch:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(author_epoch))

        commits.append({
            "sha": full_sha,
            "short_sha": short_sha,
            "author_name": author_name,
            "author_email": author_email,
            "author_epoch": author_epoch,
            "author_iso": iso,
            "author_relative": _relative(author_epoch),
            "committer_epoch": committer_epoch,
            "subject": subject,
            "body": body,
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
        })

    return commits


if __name__ == "__main__":
    # Tiny smoke test when run directly.
    ov = repo_overview()
    print(f"{len(ov)} repos discovered")
    for r in ov[:5]:
        print(f"  {r['icon']} {r['name']:20} {r['last_relative']:>10}  {r['last_subject']}")
    if ov:
        tl = commit_timeline(ov[0]["name"], limit=3)
        print(f"\n{ov[0]['name']} timeline ({len(tl)} shown):")
        for c in tl:
            print(f"  {c['short_sha']} {c['author_relative']:>10} "
                  f"+{c['insertions']}/-{c['deletions']} ({c['files_changed']}f) {c['subject']}")
