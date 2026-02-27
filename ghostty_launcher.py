#!/usr/bin/env python3
"""
Ghostty Terminal Launcher Dashboard - Web UI
A browser-based launcher with configuration and color picker.
"""

import json
import os
import subprocess
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
PORT = 8457

DEFAULT_PROJECTS = [
    {
        "name": "Example Project",
        "directory": "~/Projects/my-project",
        "background": "#0d4d4d",
        "foreground": "#ffffff",
        "icon": "📁"
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


HTML_TEMPLATE = """<!DOCTYPE html>
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

        .container { max-width: 900px; margin: 0 auto; }

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
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Ghostty Launcher</h1>
        </header>

        <div class="cards" id="cards"></div>
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
                    <input type="text" id="project-icon" placeholder="&#128193;" maxlength="4">
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
            } catch (err) {
                console.error('Failed to load projects:', err);
                projects = [];
            }
            renderCards();
        }

        function renderCards() {
            const container = document.getElementById('cards');
            if (!container) return;

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
                    <div class="card-icon">${p.icon || '&#128193;'}</div>
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
            document.getElementById('project-icon').value = '&#128193;';
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
            document.getElementById('project-icon').value = p.icon || '&#128193;';
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
                icon: document.getElementById('project-icon').value || '&#128193;',
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

        renderCards();
        loadProjects();
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

        else:
            self._send_response("Not Found", status=404)


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Ghostty Launcher running at {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
