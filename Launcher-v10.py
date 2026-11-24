"""CodeX Returns v10 launcher using a local salted password-hash file.

This is a convenience login gate for a local desktop app, not an access-control
boundary. Do not treat a public hash file as secret or as a substitute for a
server-side authentication system.
"""
from __future__ import annotations

import hashlib
import hmac
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

import webview

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "launcher_config.json"
UI_PATH = ROOT / "launcher_ui.html"
SESSION_TTL = 8 * 60 * 60
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000


def load_config() -> dict:
    config = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("launcher_config.json must contain a JSON object.")
    users_path = Path(config.get("auth_users_file", "Security/users.json"))
    config["auth_users_file"] = (ROOT / users_path).resolve() if not users_path.is_absolute() else users_path
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


def load_users(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Account file not found: {path}. Create it with create_user.py.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read account file: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("users"), list):
        raise RuntimeError("Account file has an invalid format. See V10-SETUP.md.")
    accounts = {}
    for user in data["users"]:
        if not isinstance(user, dict):
            continue
        name = str(user.get("username", "")).strip()
        if name:
            accounts[name.casefold()] = user
    return accounts


def check_password(record: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["password_hash"])
        iterations = int(record["iterations"])
        if not (16 <= len(salt) <= 64 and len(expected) == 32 and MIN_ITERATIONS <= iterations <= MAX_ITERATIONS):
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
        return hmac.compare_digest(candidate, expected)
    except (KeyError, TypeError, ValueError, UnicodeEncodeError):
        return False


def make_v10_handler(backend, state, config):
    BaseHandler = backend.make_ui_handler(state)
    users_path: Path = config["auth_users_file"]
    sessions: dict[str, tuple[str, float]] = {}
    sessions_lock = threading.Lock()

    class V10Handler(BaseHandler):
        def _send_json_with_headers(self, payload, headers=None, code=200):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

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
            if path in ("/api/auth/lookup", "/api/auth/login"):
                username = str(payload.get("username", "")).strip()[:80]
                if not username:
                    return self._send_json({"ok": False, "error": "Enter a username."}, 400)
                try:
                    user = load_users(users_path).get(username.casefold())
                except RuntimeError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, 503)
                if path == "/api/auth/lookup":
                    if not user:
                        return self._send_json({"ok": True, "registered": False})
                    return self._send_json({"ok": True, "registered": True, "display_name": str(user.get("display_name") or username)[:80]})
                password = str(payload.get("password", ""))[:1024]
                if not user or not check_password(user, password):
                    return self._send_json({"ok": False, "error": "Account or password is incorrect."}, 401)
                token = secrets.token_urlsafe(32)
                display_name = str(user.get("display_name") or username)[:80]
                with sessions_lock:
                    sessions[token] = (display_name, time.time() + SESSION_TTL)
                return self._send_json_with_headers(
                    {"ok": True, "display_name": display_name},
                    {"Set-Cookie": f"codex_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"},
                )
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
                return self._send_json_with_headers(
                    {"ok": True},
                    {"Set-Cookie": "codex_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"},
                )
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
