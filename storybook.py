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
import re
import subprocess
import time
import urllib.error
import urllib.request

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


# ---------------------------------------------------------------------------
# Phase 2 — lazy AI commit summaries (Haiku) with an immutable per-SHA cache.
#
# A commit SHA is immutable, so a summary of that SHA never needs to change:
# once generated it is cached to disk (OUTSIDE any repo) and served forever.
# Only successes are cached — errors retry next time. The Anthropic API key is
# read fresh from the macOS Keychain at call time and NEVER logged, cached in a
# global, persisted, or returned in any response.
# ---------------------------------------------------------------------------

# Config knob: bump to a Sonnet id later without touching call sites.
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MAX_TOKENS = 300

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_TIMEOUT = 45

# Keychain lookup for the summary key (service/account per project setup).
_KEYCHAIN_SERVICE = "cockpit-anthropic"
_KEYCHAIN_ACCOUNT = "storybook"

# On-disk cache, deliberately outside every repo so it is never committed.
CACHE_DIR = os.path.expanduser("~/.claude/storybook-cache")

# Diff caps — keep token cost and noise bounded.
_MAX_PATCH_LINES_PER_FILE = 400
_MAX_PATCH_BYTES = 20 * 1024  # ~20 KB total patch across the commit

# Paths whose PATCH BODY we never send to the API. They still show up in the
# --stat (filenames only). Lockfiles/vendored/generated = noise; the .env/key/
# credential entries are a security belt-and-suspenders (secrets should never
# be committed, but if one is, its contents must not leave the machine).
_EXCLUDE_GLOBS = [
    # lockfiles
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/poetry.lock", "**/Cargo.lock", "**/composer.lock",
    "**/Gemfile.lock", "**/go.sum",
    # vendored / generated trees
    "**/node_modules/**", "**/vendor/**", "**/dist/**", "**/build/**",
    # minified / generated / bundles / sourcemaps
    "**/*.min.js", "**/*.min.css", "**/*.map", "**/*.bundle.js",
    # secrets (contents excluded — filenames still visible in the stat)
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
    "**/id_rsa*", "**/credentials", "**/secrets",
]


def _read_api_key():
    """Read the Anthropic key from the Keychain. Return the key or None.

    Read fresh each call; never logged, cached, or persisted anywhere.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        # Fall back to a service-only lookup (account may not be stored).
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5)
        except Exception:
            return None
    if r.returncode != 0:
        return None
    key = (r.stdout or "").strip()
    return key or None


def _cache_path(full_sha):
    return os.path.join(CACHE_DIR, full_sha[:2], full_sha + ".json")


def _read_cache(full_sha):
    """Return the cached record dict for a SHA, or None."""
    try:
        with open(_cache_path(full_sha), "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("summary"):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _write_cache(full_sha, model, summary):
    """Persist a SUCCESSFUL summary. Best-effort; never raises."""
    try:
        path = _cache_path(full_sha)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "sha": full_sha,
                "model": model,
                "summary": summary,
                "generated_at_epoch": int(time.time()),
            }, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _commit_stat(root, full_sha):
    """`git show --stat` (name + insertions/deletions per file, includes
    binaries/lockfiles by filename). Returns text or ''."""
    res = _run_git([
        "-C", root, "show", "--stat", "--format=", full_sha,
    ], timeout=15)
    if res is None or res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def _commit_patch(root, full_sha):
    """Filtered, bounded unified diff for a commit.

    Uses `:(exclude,glob)` pathspecs so excluded/secret files never contribute
    patch bytes, then truncates to <=400 lines/file and <=~20 KB total.
    Returns (patch_text, truncated_bool).
    """
    pathspec = ["--", "."] + [f":(exclude,glob){g}" for g in _EXCLUDE_GLOBS]
    res = _run_git([
        "-C", root, "show", "--format=", "--unified=3", full_sha, *pathspec,
    ], timeout=20)
    if res is None or res.returncode != 0:
        return "", False
    raw = res.stdout or ""
    if not raw.strip():
        return "", False

    # Split into per-file blocks on `diff --git` boundaries.
    blocks = []
    current = []
    for line in raw.split("\n"):
        if line.startswith("diff --git ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    out_parts = []
    total = 0
    truncated = False
    for block in blocks:
        if len(block) > _MAX_PATCH_LINES_PER_FILE:
            block = block[:_MAX_PATCH_LINES_PER_FILE]
            block.append("... (file diff truncated)")
            truncated = True
        chunk = "\n".join(block)
        if total + len(chunk) > _MAX_PATCH_BYTES:
            remaining = _MAX_PATCH_BYTES - total
            if remaining > 0:
                out_parts.append(chunk[:remaining])
            truncated = True
            break
        out_parts.append(chunk)
        total += len(chunk) + 1
    patch = "\n".join(out_parts).strip()
    return patch, truncated


def _commit_message(root, full_sha):
    """Return (subject, body) for a SHA."""
    res = _run_git([
        "-C", root, "show", "-s", "--format=%s" + _RS + "%b", full_sha,
    ], timeout=10)
    if res is None or res.returncode != 0:
        return "", ""
    parts = (res.stdout or "").split(_RS, 1)
    subject = parts[0].strip() if parts else ""
    body = parts[1].strip() if len(parts) > 1 else ""
    return subject, body


_SUMMARY_SYSTEM = (
    "You are a precise software-history narrator. Given a single git commit "
    "(its message plus a filtered, possibly truncated diff), explain what the "
    "change actually does in plain language for a developer skimming project "
    "history. Stay strictly grounded in the evidence shown: the commit message "
    "and the diff. Do NOT invent motivation, features, or files that are not "
    "present; only mention 'why' if it is stated in the message or clearly "
    "implied by the diff. If the diff was truncated, summarize what is visible "
    "and do not guess at the rest. Be concise and concrete."
)


def _build_summary_prompt(subject, body, stat, patch, truncated):
    parts = ["Summarize this commit.\n"]
    parts.append("=== Commit subject ===\n" + (subject or "(none)"))
    if body:
        parts.append("\n=== Commit body ===\n" + body)
    if stat:
        parts.append("\n=== Files changed (git show --stat) ===\n" + stat)
    if patch:
        note = " (NOTE: diff truncated)" if truncated else ""
        parts.append(f"\n=== Filtered diff{note} ===\n" + patch)
    else:
        parts.append("\n=== Filtered diff ===\n(no textual diff available — "
                     "binary, excluded, or empty change)")
    parts.append(
        "\n=== Your task ===\n"
        "Write one or two plain-language sentences describing what this change "
        "does (add 'why' ONLY if the message/diff makes it clear — never "
        "invent a reason). Then up to 3 short bullet points of concrete "
        "specifics (files/areas touched, notable additions or removals). Keep "
        "it grounded in the diff above; do not restate the subject verbatim — "
        "add interpretation. Plain text / light markdown only."
    )
    return "\n".join(parts)


def _call_anthropic(key, system, prompt):
    """POST to the Messages API and return the text. Raises on failure.

    The key travels only in the request header — never logged or returned.
    """
    body = json.dumps({
        "model": SUMMARY_MODEL,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        _ANTHROPIC_URL, data=body, method="POST", headers={
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=_ANTHROPIC_TIMEOUT) as r:
        payload = json.loads(r.read().decode())
    content = payload.get("content") or []
    if content and isinstance(content, list):
        text = content[0].get("text", "")
        if text:
            return text
    raise RuntimeError("empty-response")


def commit_summary(project_name, sha):
    """Return {sha, summary, cached} for a commit, generating + caching lazily.

    On ANY failure returns {sha, summary: None, error: <short reason>} with a
    reason string that contains NO key material. Never raises. Only successful
    summaries are cached (errors retry on the next request).
    """
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return {"sha": sha, "summary": None, "error": "bad-sha"}

    repo = _resolve_project(project_name)
    if repo is None:
        return {"sha": sha, "summary": None, "error": "unknown-project"}

    # Resolve to a full, verified commit sha inside this repo (never a path).
    res = _run_git(["-C", repo["root"], "rev-parse", "--verify",
                    "--quiet", sha + "^{commit}"])
    if res is None or res.returncode != 0 or not res.stdout.strip():
        return {"sha": sha, "summary": None, "error": "unknown-commit"}
    full_sha = res.stdout.strip()

    cached = _read_cache(full_sha)
    if cached:
        return {"sha": full_sha, "summary": cached["summary"], "cached": True}

    key = _read_api_key()
    if not key:
        return {"sha": full_sha, "summary": None, "error": "no-api-key"}

    try:
        subject, body = _commit_message(repo["root"], full_sha)
        stat = _commit_stat(repo["root"], full_sha)
        patch, truncated = _commit_patch(repo["root"], full_sha)
        prompt = _build_summary_prompt(subject, body, stat, patch, truncated)
    except Exception:
        return {"sha": full_sha, "summary": None, "error": "git-error"}

    try:
        text = _call_anthropic(key, _SUMMARY_SYSTEM, prompt)
    except urllib.error.HTTPError as e:
        # Status code only — never the body/headers (belt-and-suspenders).
        return {"sha": full_sha, "summary": None, "error": f"api-http-{e.code}"}
    except urllib.error.URLError:
        return {"sha": full_sha, "summary": None, "error": "api-network"}
    except Exception:
        return {"sha": full_sha, "summary": None, "error": "api-error"}
    finally:
        del key  # drop the reference promptly

    text = (text or "").strip()
    if not text:
        return {"sha": full_sha, "summary": None, "error": "empty-summary"}

    _write_cache(full_sha, SUMMARY_MODEL, text)
    return {"sha": full_sha, "summary": text, "cached": False}


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
