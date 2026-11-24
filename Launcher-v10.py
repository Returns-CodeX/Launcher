"""CodeX Returns v10 entry point and optional remote-auth gateway.

This wrapper reuses the established game downloader in CodeX-Returns.py and
serves launcher_ui.html as the new launcher interface. It does not store user
credentials or GitHub tokens. Configure CODEX_AUTH_API_URL (HTTPS) for an
independent authentication service implementing the contract in V10-SETUP.md.
"""
from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
import webview

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "launcher_config.json"
UI_PATH = ROOT / "launcher_ui.html"
SESSION_TTL = 8 * 60 * 60


def load_config() -> dict:
    config = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("launcher_config.json must contain a JSON object.")
    api_url = os.environ.get("CODEX_AUTH_API_URL", config.get("auth_api_url", ""))
    if api_url:
        parsed = urlparse(api_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("auth_api_url must be an HTTPS URL.")
    config["auth_api_url"] = api_url.rstrip("/")
    return config


def load_legacy_backend():
    path = ROOT / "CodeX-Returns.py"
    spec = importlib.util.spec_from_file_location("codex_returns_backend", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load existing game engine: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_v10_handler(backend, state, config):
    BaseHandler = backend.make_ui_handler(state)
    auth_api = config.get("auth_api_url", "")
    sessions: dict[str, tuple[str, float]] = {}
    sessions_lock = threading.Lock()

    class V10Handler(BaseHandler):
        def _body(self):
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 16_384)
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return {}

        def _authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie.get("codex_session")
            except Exception:
                return False
            if not token:
                return False
            now = time.time()
            with sessions_lock:
                record = sessions.get(token.value)
                if not record or record[1] < now:
                    sessions.pop(token.value, None)
                    return False
                sessions[token.value] = (record[0], now + SESSION_TTL)
            return True

        def _remote_auth(self, endpoint, payload):
            if not auth_api:
                self._send_json({"ok": False, "error": "Remote sign-in is not configured. Set CODEX_AUTH_API_URL."}, 503)
                return None
            try:
                response = requests.post(f"{auth_api}/{endpoint}", json=payload, timeout=12)
                if response.status_code in (401, 404):
                    return {"ok": False, "error": "Account not found or credentials are incorrect."}
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Authentication service returned an invalid response.")
                return data
            except (requests.RequestException, ValueError) as exc:
                self._send_json({"ok": False, "error": f"Authentication service unavailable: {exc}"}, 502)
                return None

        def do_GET(self):
            if self.path.split("?", 1)[0] in ("/", "/index.html"):
                try:
                    body = UI_PATH.read_bytes()
                except OSError:
                    return self._send_json({"error": "launcher_ui.html is missing."}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/") and not self._authenticated():
                return self._send_json({"error": "Sign in required."}, 401)
            return super().do_GET()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            payload = self._body()
            if path == "/api/auth/lookup":
                data = self._remote_auth("lookup", {"username": str(payload.get("username", ""))[:80]})
                if data is not None and data.get("ok"):
                    return self._send_json({"ok": True, "registered": bool(data.get("registered")), "display_name": str(data.get("display_name", ""))[:80]})
                if data is not None:
                    return self._send_json(data, 401)
                return
            if path == "/api/auth/login":
                username = str(payload.get("username", ""))[:80]
                password = str(payload.get("password", ""))[:1024]
                data = self._remote_auth("login", {"username": username, "password": password})
                if data is None:
                    return
                if not data.get("ok"):
                    return self._send_json({"ok": False, "error": str(data.get("error", "Incorrect password."))[:200]}, 401)
                token = secrets.token_urlsafe(32)
                with sessions_lock:
                    sessions[token] = (str(data.get("display_name") or username), time.time() + SESSION_TTL)
                self._send_json({"ok": True, "display_name": str(data.get("display_name") or username)[:80]}, 200)
                self.send_header("Set-Cookie", f"codex_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")
                return
            if path == "/api/auth/logout":
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                    token = cookie.get("codex_session")
                    if token:
                        with sessions_lock:
                            sessions.pop(token.value, None)
                except Exception:
                    pass
                self._send_json({"ok": True})
                self.send_header("Set-Cookie", "codex_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if path.startswith("/api/") and not self._authenticated():
                return self._send_json({"error": "Sign in required."}, 401)
            return super().do_POST()

    return V10Handler


def main():
    config = load_config()
    backend = load_legacy_backend()
    backend.GITHUB_REPO_URL = config.get("games_repo_url", backend.GITHUB_REPO_URL)
    backend.BRANCH = config.get("games_branch", backend.BRANCH)
    backend.GAMES_FOLDER = config.get("games_folder", backend.GAMES_FOLDER)
    backend.hide_console_window()
    state = backend.LauncherState()
    port, _ = backend.find_free_port(None)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_v10_handler(backend, state, config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    window = webview.create_window(
        title="CodeX-Returns // Arcade",
        url=f"http://127.0.0.1:{port}/",
        width=1440,
        height=920,
        min_size=(900, 640),
        maximized=True,
        background_color="#08090d",
    )
    try:
        webview.start(func=lambda: backend.maximize_app_window(window))
    finally:
        state.stop_all()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
