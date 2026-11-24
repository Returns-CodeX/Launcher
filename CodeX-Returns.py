from __future__ import annotations

import ctypes
import json
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import webview


# =============================================================================
# Launcher configuration
# =============================================================================

GITHUB_REPO_URL = "https://github.com/Returns-CodeX/CodeX-files"
BRANCH = "main"
GAMES_FOLDER = "files"
BASE_PORT = 8000
MAX_WORKERS = 12
REQUEST_TIMEOUT = 30

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
GAMES_CACHE_DIR = CACHE_DIR / "games"
INDEX_CACHE_PATH = CACHE_DIR / "games_index.json"
LAST_QUOTE_PATH = CACHE_DIR / "last_quote.json"

QUOTES = [
    ('"Stay sharp and keep building."', "CODEX"),
    ('"Every run is a new beginning."', "CODEX"),
    ('"Find your next favorite."', "CODEX"),
    ('"Press play. See what happens."', "CODEX"),
]

# The game-list endpoint can return only this many entries per response from
# the GitHub API. For a very large repository, use the recursive git tree API.
MAX_JSON_BODY_BYTES = 16_384


def hide_console_window() -> None:
    """Hide the terminal window on Windows when running as a desktop app."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def maximize_app_window(window) -> None:
    """Try to maximize the pywebview window without making startup fail."""
    try:
        window.maximize()
    except Exception:
        pass


def pick_next_quote() -> dict:
    """Choose a quote, avoiding the previous one when possible."""
    if not QUOTES:
        return {"lines": ["Stay sharp and keep building."], "author": "CODEX"}

    previous = None
    try:
        saved = json.loads(LAST_QUOTE_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            previous = saved.get("index")
    except (OSError, json.JSONDecodeError):
        pass

    choices = list(range(len(QUOTES)))
    if len(choices) > 1 and previous in choices:
        choices.remove(previous)

    index = random.choice(choices)
    try:
        LAST_QUOTE_PATH.write_text(
            json.dumps({"index": index}),
            encoding="utf-8",
        )
    except OSError:
        pass

    text, author = QUOTES[index]
    return {"lines": text.splitlines(), "author": author}


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Accept an owner/repo name or a github.com URL."""
    cleaned = repo_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    match = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", cleaned)
    if match:
        return match.group(1), match.group(2)

    parts = cleaned.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]

    raise ValueError(
        "GITHUB_REPO_URL must be a GitHub URL or an owner/repository name."
    )


def build_session(token: str = "") -> requests.Session:
    """Create a reusable GitHub API session.

    Public repositories work without a token. For private repositories, set
    GITHUB_TOKEN in the environment; never put a token in this source file.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "CodeX-Returns-Launcher/10.0",
            "Accept": "application/vnd.github+json",
        }
    )

    token = token.strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS,
    )
    session.mount("https://", adapter)
    return session


def find_free_port(preferred: int | None = None) -> tuple[int, bool]:
    """Return a free localhost TCP port and whether the preferred port worked."""
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred, True
            except OSError:
                pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1]), False


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024**3:
        return f"{num_bytes / (1024**2):.2f} MB"
    return f"{num_bytes / (1024**3):.2f} GB"


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "0 KB/s"
    if bytes_per_second >= 1024**2:
        return f"{bytes_per_second / (1024**2):.2f} MB/s"
    return f"{bytes_per_second / 1024:.1f} KB/s"


def copy_to_clipboard(text: str) -> bool:
    """Best-effort system clipboard support."""
    if not text:
        return False

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["clip"],
                input=text.encode("utf-16le"),
                capture_output=True,
                timeout=3,
                check=False,
            )
            return result.returncode == 0

        if sys.platform == "darwin":
            result = subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=3,
                check=False,
            )
            return result.returncode == 0

        for command in (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                result = subprocess.run(
                    command,
                    input=text.encode("utf-8"),
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
                if result.returncode == 0:
                    return True
            except FileNotFoundError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass

    return False


def parse_lfs_pointer(data: bytes) -> tuple[str, int] | None:
    """Return the SHA-256 object ID and size for a Git LFS pointer."""
    if not data or len(data) > 2048:
        return None
    if b"https://git-lfs.github.com/spec/v1" not in data:
        return None

    text = data.decode("utf-8", errors="ignore")
    oid = re.search(r"^oid sha256:([0-9a-f]{64})\s*$", text, re.MULTILINE)
    size = re.search(r"^size (\d+)\s*$", text, re.MULTILINE)
    if not oid or not size:
        return None

    return oid.group(1), int(size.group(1))


def resolve_lfs_download_url(
    session: requests.Session,
    owner: str,
    repo: str,
    oid: str,
    size: int,
) -> str:
    """Resolve a Git LFS file pointer to its actual download URL."""
    url = f"https://github.com/{quote(owner)}/{quote(repo)}.git/info/lfs/objects/batch"
    response = session.post(
        url,
        json={
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": size}],
        },
        headers={
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    objects = response.json().get("objects") or []
    if not objects:
        raise RuntimeError(f"Git LFS returned no object for {oid[:12]}.")

    item = objects[0]
    if item.get("error"):
        raise RuntimeError(item["error"].get("message", "Git LFS download failed."))

    download_url = (item.get("actions", {}).get("download") or {}).get("href")
    if not download_url:
        raise RuntimeError("Git LFS response did not include a download URL.")

    return download_url


BROWSER_BOOT_HTML = """
<style id="codex-boot-style">
#codex-browser-boot {
    position: fixed; inset: 0; z-index: 2147483647;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 14px;
    color: #f5f5f5; background: radial-gradient(circle, #21152f, #08090d 76%);
    font: 700 14px system-ui, sans-serif;
    transition: opacity .55s ease, transform .55s ease;
}
#codex-browser-boot.is-done { opacity: 0; transform: scale(1.025); pointer-events: none; }
#codex-browser-boot .logo {
    color: #b69aff; font-size: clamp(30px, 6vw, 62px);
    letter-spacing: 5px; text-shadow: 0 0 24px #a889ff99;
}
#codex-browser-boot .bar {
    width: min(360px, 75vw); height: 6px; border-radius: 999px;
    overflow: hidden; background: #ffffff20;
}
#codex-browser-boot .fill {
    width: 100%; height: 100%; transform-origin: left;
    background: linear-gradient(90deg, #7161ef, #6be7dc);
    animation: codexLoad 1.1s ease forwards;
}
@keyframes codexLoad { from { transform: scaleX(.04) } to { transform: scaleX(1) } }
</style>
<div id="codex-browser-boot" aria-hidden="true">
    <div style="letter-spacing:3px;color:#6be7dc">CODEX ARCADE</div>
    <div class="logo">GET READY</div>
    <div class="bar"><div class="fill"></div></div>
    <div style="color:#b4b5c3;letter-spacing:2px">LOADING GAME</div>
</div>
<script>
(function () {
    function dismiss() {
        const overlay = document.getElementById("codex-browser-boot");
        if (!overlay) return;
        overlay.classList.add("is-done");
        setTimeout(() => {
            overlay.remove();
            document.getElementById("codex-boot-style")?.remove();
        }, 600);
    }
    if (document.readyState === "complete") {
        setTimeout(dismiss, 800);
    } else {
        window.addEventListener("load", () => setTimeout(dismiss, 800), { once: true });
        setTimeout(dismiss, 2500);
    }
})();
</script>
"""


class GameBootHTTPHandler(SimpleHTTPRequestHandler):
    """Serve a cached game folder and add a lightweight boot animation."""

    def log_message(self, _format, *_args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        clean_path = parsed.path or "/index.html"
        if clean_path == "/":
            clean_path = "/index.html"

        file_path = Path(self.translate_path(clean_path))
        if file_path.is_file() and file_path.suffix.lower() in {".html", ".htm"}:
            try:
                html = file_path.read_text(encoding="utf-8", errors="replace")
                if "codex-browser-boot" not in html:
                    body_tag = re.search(r"<body[^>]*>", html, re.IGNORECASE)
                    if body_tag:
                        insertion = body_tag.end()
                        html = html[:insertion] + BROWSER_BOOT_HTML + html[insertion:]
                    else:
                        html = BROWSER_BOOT_HTML + html

                payload = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except OSError:
                pass

        super().do_GET()


class QuietUIHandler(SimpleHTTPRequestHandler):
    """Base handler for the launcher's local web UI."""

    def log_message(self, _format, *_args):
        pass

# =============================================================================
# Launcher state and game repository engine
# =============================================================================

class LauncherState:
    def __init__(self) -> None:
        self.games: list[dict] = []
        self.quote = pick_next_quote()
        self.status = f"Repository: {GITHUB_REPO_URL} | Cache: .cache/"
        self.servers: dict[int, ThreadingHTTPServer] = {}
        self.game_ports: dict[int, int] = {}
        self.port_info: dict[int, dict] = {}
        self.progress = {
            "active": False,
            "stage": "Idle",
            "detail": "",
            "percent": 0.0,
            "speed": "0 KB/s",
            "filesDone": 0,
            "filesTotal": 0,
            "startedAt": 0.0,
        }

        self.lock = threading.RLock()
        self.launch_lock = threading.Lock()
        self.progress_lock = threading.Lock()

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        GAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if INDEX_CACHE_PATH.exists():
            try:
                cached_games = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(cached_games, list):
                    self.games = cached_games
                    self.status = (
                        f"Loaded {len(cached_games)} cached games. "
                        "Choose SYNC LIBRARY for the newest list."
                    )
            except (OSError, json.JSONDecodeError):
                pass

    def next_quote(self) -> dict:
        with self.lock:
            self.quote = pick_next_quote()
            return dict(self.quote)

    def active_ports(self) -> list[dict]:
        with self.lock:
            return [
                dict(info)
                for port, info in sorted(self.port_info.items())
                if port in self.servers
            ]

    def set_progress(self, **values) -> None:
        with self.progress_lock:
            self.progress.update(values)

    def get_progress(self) -> dict:
        with self.progress_lock:
            return dict(self.progress)

    def serialize_games(self) -> list[dict]:
        """Return UI-safe game metadata while holding the state lock."""
        with self.lock:
            result = []
            for game in self.games:
                name = str(game["name"])
                number = int(game["num"])
                dest = GAMES_CACHE_DIR / name
                files = game.get("files", [])
                sizes = game.get("file_sizes", {})

                file_count = len(files)
                total_bytes = 0

                if file_count:
                    for relative in files:
                        local_file = dest / relative
                        if local_file.is_file():
                            total_bytes += local_file.stat().st_size
                        elif isinstance(sizes, dict):
                            reported = sizes.get(relative)
                            if isinstance(reported, int):
                                total_bytes += reported
                elif dest.exists():
                    local_files = [item for item in dest.rglob("*") if item.is_file()]
                    file_count = len(local_files)
                    total_bytes = sum(item.stat().st_size for item in local_files)

                result.append(
                    {
                        "num": number,
                        "name": name,
                        "port": self.game_ports.get(number, BASE_PORT + number),
                        "cached": (dest / "index.html").is_file(),
                        "file_count": file_count,
                        "total_bytes": total_bytes,
                        "size_formatted": format_size(total_bytes),
                    }
                )

            return result

    def stop_server(self, httpd: ThreadingHTTPServer | None) -> None:
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass

    def stop_port(self, port: int) -> dict:
        with self.lock:
            httpd = self.servers.pop(port, None)
            info = self.port_info.pop(port, None)

            if info and self.game_ports.get(info["num"]) == port:
                self.game_ports.pop(info["num"], None)

            if httpd:
                label = f" (#{info['num']} {info['name']})" if info else ""
                self.status = f"Stopped port {port}{label}"
                ok = True
            else:
                self.status = f"Port {port} was not active."
                ok = False

            snapshot = {
                "ok": ok,
                "active_ports": self.active_ports(),
                "games": self.serialize_games(),
                "status": self.status,
            }

        self.stop_server(httpd)
        return snapshot

    def stop_all(self) -> None:
        with self.lock:
            servers = list(self.servers.values())
            self.servers.clear()
            self.game_ports.clear()
            self.port_info.clear()

        for server in servers:
            self.stop_server(server)

    def refresh_games(self) -> dict:
        """Refresh the <GAMES_FOLDER>/ game directory listing from GitHub."""
        with self.launch_lock:
            try:
                owner, repo = parse_github_repo(GITHUB_REPO_URL)
                session = build_session()

                tree = None
                branch_used = BRANCH
                branches = [BRANCH]
                fallback = "master" if BRANCH != "master" else "main"
                if fallback not in branches:
                    branches.append(fallback)

                for branch in branches:
                    api_url = (
                        f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
                        f"/git/trees/{quote(branch)}?recursive=1"
                    )
                    response = session.get(api_url, timeout=REQUEST_TIMEOUT)

                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    tree = response.json()
                    branch_used = branch
                    break

                if not tree or "tree" not in tree:
                    raise RuntimeError(
                        "Could not load the repository tree from 'main' or 'master'."
                    )

                prefix = GAMES_FOLDER.strip("/") + "/"
                folder_names: set[str] = set()
                file_map: dict[str, list[str]] = {}
                size_map: dict[str, dict[str, int]] = {}

                for item in tree["tree"]:
                    path = str(item.get("path", ""))
                    if not path.startswith(prefix):
                        continue

                    relative = path[len(prefix):]
                    parts = relative.split("/")
                    game_name = parts[0].strip()
                    if not game_name:
                        continue

                    folder_names.add(game_name)

                    if item.get("type") == "blob" and len(parts) >= 2:
                        sub_relative = "/".join(parts[1:])
                        file_map.setdefault(game_name, []).append(sub_relative)
                        if isinstance(item.get("size"), int):
                            size_map.setdefault(game_name, {})[sub_relative] = int(
                                item["size"]
                            )

                sorted_names = sorted(folder_names, key=str.lower)
                new_games = [
                    {
                        "num": index,
                        "name": name,
                        "branch": branch_used,
                        "files": sorted(file_map.get(name, [])),
                        "file_sizes": size_map.get(name, {}),
                    }
                    for index, name in enumerate(sorted_names, start=1)
                ]

                INDEX_CACHE_PATH.write_text(
                    json.dumps(new_games, indent=2),
                    encoding="utf-8",
                )

                with self.lock:
                    self.games = new_games
                    self.status = (
                        f"Found {len(new_games)} games in {GAMES_FOLDER}/ "
                        f"on {branch_used}. Library cached locally."
                    )
                    return {
                        "ok": True,
                        "games": self.serialize_games(),
                        "active_ports": self.active_ports(),
                        "status": self.status,
                    }

            except Exception as exc:
                with self.lock:
                    self.status = f"Repository sync failed: {exc}"
                    return {
                        "ok": False,
                        "games": self.serialize_games(),
                        "active_ports": self.active_ports(),
                        "status": self.status,
                    }

    def collect_files(
        self,
        session: requests.Session,
        owner: str,
        repo: str,
        branch: str,
        folder_name: str,
    ) -> tuple[list[str], dict[str, int]]:
        """Fallback recursive directory collection for a game."""
        found: list[str] = []
        sizes: dict[str, int] = {}
        base_folder = f"{GAMES_FOLDER.strip('/')}/{folder_name}".strip("/")

        def walk(relative_folder: str) -> None:
            target = (
                f"{base_folder}/{relative_folder}".strip("/")
                if relative_folder
                else base_folder
            )
            api_url = (
                f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
                f"/contents/{quote(target, safe='/')}?ref={quote(branch)}"
            )
            response = session.get(api_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            entries = response.json()

            if not isinstance(entries, list):
                return

            for entry in entries:
                entry_type = entry.get("type")
                entry_name = str(entry.get("name", "")).strip("/")
                if not entry_name:
                    continue

                next_relative = (
                    f"{relative_folder}/{entry_name}".strip("/")
                    if relative_folder
                    else entry_name
                )

                if entry_type == "file":
                    found.append(next_relative)
                    if isinstance(entry.get("size"), int):
                        sizes[next_relative] = int(entry["size"])
                elif entry_type == "dir":
                    walk(next_relative)

        walk("")
        return sorted(found), sizes

    def launch_game(
        self,
        number: int,
        force_redownload: bool = False,
    ) -> dict:
        """Download/cache a game, start its localhost server, and return its URL."""
        with self.launch_lock:
            started = time.monotonic()
            downloaded_bytes = 0
            completed_files = 0
            skipped_files = 0
            total_files = 0
            desired_port = BASE_PORT + number

            self.set_progress(
                active=True,
                stage="Resolving game",
                detail=f"Finding Game #{number}...",
                percent=2.0,
                speed="0 KB/s",
                filesDone=0,
                filesTotal=0,
                startedAt=started,
            )

            try:
                with self.lock:
                    game = next(
                        (item for item in self.games if item["num"] == number),
                        None,
                    )
                if not game:
                    raise ValueError(f"Game #{number} was not found in the game index.")

                owner, repo = parse_github_repo(GITHUB_REPO_URL)
                session = build_session()
                name = str(game["name"])
                branch = str(game.get("branch", BRANCH))
                files = list(game.get("files", []))
                expected_sizes = dict(game.get("file_sizes", {}))

                dest_dir = GAMES_CACHE_DIR / name
                dest_dir.mkdir(parents=True, exist_ok=True)
                resolved_dest = dest_dir.resolve()

                if not files:
                    self.set_progress(
                        active=True,
                        stage="Scanning repository",
                        detail=f"Reading folder: {name}",
                        percent=6.0,
                        speed="0 KB/s",
                        filesDone=0,
                        filesTotal=0,
                        startedAt=started,
                    )
                    files, discovered_sizes = self.collect_files(
                        session,
                        owner,
                        repo,
                        branch,
                        name,
                    )
                    game["files"] = files
                    game["file_sizes"] = discovered_sizes
                    expected_sizes = discovered_sizes

                total_files = len(files)
                if not total_files:
                    raise FileNotFoundError(
                        f"No files were found inside {GAMES_FOLDER}/{name}/."
                    )

                progress_lock = threading.Lock()

                def fetch_one(relative_path: str) -> None:
                    nonlocal downloaded_bytes, completed_files, skipped_files

                    output_file = (dest_dir / relative_path).resolve()
                    if not output_file.is_relative_to(resolved_dest):
                        raise ValueError(
                            f"Unsafe relative file path: {relative_path}"
                        )

                    if not force_redownload and output_file.is_file():
                        existing = output_file.read_bytes()
                        if parse_lfs_pointer(existing) is None:
                            expected = expected_sizes.get(relative_path)
                            if (expected is None and len(existing) > 0) or (
                                isinstance(expected, int)
                                and len(existing) == expected
                            ):
                                with progress_lock:
                                    completed_files += 1
                                    skipped_files += 1
                                    elapsed = max(time.monotonic() - started, 0.001)
                                    speed = format_speed(downloaded_bytes / elapsed)
                                    percent = min(
                                        94.0,
                                        8.0
                                        + (completed_files / total_files) * 86.0,
                                    )
                                self.set_progress(
                                    active=True,
                                    stage="Checking cache",
                                    detail=f"Cached: {relative_path}",
                                    percent=percent,
                                    speed=speed,
                                    filesDone=completed_files,
                                    filesTotal=total_files,
                                    startedAt=started,
                                )
                                return

                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    encoded_relative = "/".join(
                        quote(part) for part in relative_path.split("/")
                    )
                    raw_url = (
                        f"https://raw.githubusercontent.com/"
                        f"{quote(owner)}/{quote(repo)}/{quote(branch)}/"
                        f"{quote(GAMES_FOLDER.strip('/'))}/{quote(name)}/"
                        f"{encoded_relative}"
                    )

                    self.set_progress(
                        active=True,
                        stage="Downloading",
                        detail=f"Fetching {relative_path}",
                    )

                    with session.get(
                        raw_url,
                        stream=True,
                        timeout=REQUEST_TIMEOUT,
                    ) as response:
                        response.raise_for_status()
                        chunks: list[bytes] = []

                        for chunk in response.iter_content(chunk_size=32768):
                            if not chunk:
                                continue
                            chunks.append(chunk)

                            with progress_lock:
                                downloaded_bytes += len(chunk)
                                elapsed = max(time.monotonic() - started, 0.001)
                                speed = format_speed(downloaded_bytes / elapsed)
                                percent = min(
                                    92.0,
                                    8.0
                                    + (
                                        (completed_files + 0.5)
                                        / max(total_files, 1)
                                    )
                                    * 84.0,
                                )

                            self.set_progress(
                                active=True,
                                stage="Downloading",
                                detail=f"Fetching {relative_path}",
                                percent=percent,
                                speed=speed,
                                filesDone=completed_files,
                                filesTotal=total_files,
                                startedAt=started,
                            )

                    raw_bytes = b"".join(chunks)
                    lfs_pointer = parse_lfs_pointer(raw_bytes)

                    if lfs_pointer:
                        oid, lfs_size = lfs_pointer
                        self.set_progress(
                            active=True,
                            stage="Git LFS",
                            detail=f"Resolving {relative_path}",
                        )

                        lfs_url = resolve_lfs_download_url(
                            session,
                            owner,
                            repo,
                            oid,
                            lfs_size,
                        )

                        with session.get(
                            lfs_url,
                            stream=True,
                            timeout=60,
                        ) as lfs_response:
                            lfs_response.raise_for_status()
                            lfs_chunks: list[bytes] = []

                            for chunk in lfs_response.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                lfs_chunks.append(chunk)

                                with progress_lock:
                                    downloaded_bytes += len(chunk)
                                    elapsed = max(time.monotonic() - started, 0.001)
                                    speed = format_speed(
                                        downloaded_bytes / elapsed
                                    )
                                    percent = min(
                                        92.0,
                                        8.0
                                        + (
                                            (completed_files + 0.5)
                                            / max(total_files, 1)
                                        )
                                        * 84.0,
                                    )

                                self.set_progress(
                                    active=True,
                                    stage="Downloading",
                                    detail=f"Fetching LFS file {relative_path}",
                                    percent=percent,
                                    speed=speed,
                                    filesDone=completed_files,
                                    filesTotal=total_files,
                                    startedAt=started,
                                )

                        raw_bytes = b"".join(lfs_chunks)

                    output_file.write_bytes(raw_bytes)

                    with progress_lock:
                        completed_files += 1
                        elapsed = max(time.monotonic() - started, 0.001)
                        speed = format_speed(downloaded_bytes / elapsed)
                        percent = min(
                            94.0,
                            8.0
                            + (completed_files / max(total_files, 1)) * 86.0,
                        )

                    self.set_progress(
                        active=True,
                        stage="Downloading",
                        detail=f"Downloaded {relative_path}",
                        percent=percent,
                        speed=speed,
                        filesDone=completed_files,
                        filesTotal=total_files,
                        startedAt=started,
                    )

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = [
                        pool.submit(fetch_one, relative)
                        for relative in files
                    ]
                    for future in as_completed(futures):
                        future.result()

                elapsed = max(time.monotonic() - started, 0.001)
                speed = format_speed(downloaded_bytes / elapsed)

                self.set_progress(
                    active=True,
                    stage="Starting server",
                    detail=f"Verifying {name}/index.html",
                    percent=97.0,
                    speed=speed,
                    filesDone=completed_files,
                    filesTotal=total_files,
                    startedAt=started,
                )

                index_file = dest_dir / "index.html"
                if not index_file.is_file():
                    raise FileNotFoundError(
                        f"index.html was not found in .cache/games/{name}/"
                    )

                old_server = None
                with self.lock:
                    old_port = self.game_ports.get(number)
                    if old_port in self.servers:
                        old_server = self.servers.pop(old_port, None)
                        self.port_info.pop(old_port, None)
                        self.game_ports.pop(number, None)

                self.stop_server(old_server)

                actual_port, exact_match = find_free_port(desired_port)

                self.set_progress(
                    active=True,
                    stage="Starting server",
                    detail=(
                        f"Starting localhost:{actual_port}"
                        if exact_match
                        else f"Preferred port busy; using localhost:{actual_port}"
                    ),
                    percent=99.0,
                    speed=speed,
                    filesDone=completed_files,
                    filesTotal=total_files,
                    startedAt=started,
                )

                handler = partial(
                    GameBootHTTPHandler,
                    directory=str(dest_dir),
                )
                server = ThreadingHTTPServer(
                    ("127.0.0.1", actual_port),
                    handler,
                )
                threading.Thread(
                    target=server.serve_forever,
                    daemon=True,
                ).start()

                url = f"http://127.0.0.1:{actual_port}/index.html"
                copied = copy_to_clipboard(url)
                clip_status = "Link copied" if copied else "Copy link below"

                with self.lock:
                    self.servers[actual_port] = server
                    self.game_ports[number] = actual_port
                    self.port_info[actual_port] = {
                        "port": actual_port,
                        "num": number,
                        "name": name,
                        "url": url,
                    }
                    self.status = (
                        f"#{number} {name} is live on port {actual_port}. "
                        f"{clip_status}"
                    )
                    games_snapshot = self.serialize_games()
                    ports_snapshot = self.active_ports()

                final_progress = {
                    "active": True,
                    "stage": "Ready",
                    "detail": f"{name} is ready on {url}",
                    "percent": 100.0,
                    "speed": speed,
                    "filesDone": completed_files,
                    "filesTotal": total_files,
                    "startedAt": started,
                }
                self.set_progress(**final_progress)

                return {
                    "ok": True,
                    "url": url,
                    "port": actual_port,
                    "copied": copied,
                    "progress": final_progress,
                    "games": games_snapshot,
                    "active_ports": ports_snapshot,
                    "status": self.status,
                }

            except Exception as exc:
                elapsed = max(time.monotonic() - started, 0.001)
                speed = format_speed(downloaded_bytes / elapsed)
                error_progress = {
                    "active": True,
                    "stage": "Launch failed",
                    "detail": str(exc),
                    "percent": 0.0,
                    "speed": speed,
                    "filesDone": completed_files,
                    "filesTotal": total_files,
                    "startedAt": started,
                }
                self.set_progress(**error_progress)

                with self.lock:
                    self.status = f"Launch failed: {exc}"
                    games_snapshot = self.serialize_games()
                    ports_snapshot = self.active_ports()

                return {
                    "ok": False,
                    "progress": error_progress,
                    "games": games_snapshot,
                    "active_ports": ports_snapshot,
                    "status": self.status,
                }

# =============================================================================
# No-login launcher interface
# =============================================================================

HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeX Returns Arcade</title>
<style>
:root {
    --bg: #090a10;
    --panel: rgba(18, 20, 30, .86);
    --panel-2: #151724;
    --line: rgba(255,255,255,.1);
    --text: #f5f6fb;
    --muted: #9296aa;
    --purple: #a889ff;
    --cyan: #6be7dc;
    --pink: #ff77bb;
    --green: #78e6ae;
}

* { box-sizing: border-box; }

html, body {
    min-height: 100%;
    margin: 0;
}

body {
    overflow-x: hidden;
    color: var(--text);
    background: var(--bg);
    font-family: Arial, sans-serif;
}

button, input {
    font: inherit;
}

button {
    cursor: pointer;
}

button:disabled {
    cursor: not-allowed;
    opacity: .55;
}

#particles {
    position: fixed;
    inset: 0;
    z-index: -1;
    width: 100%;
    height: 100%;
}

.background {
    position: fixed;
    inset: 0;
    z-index: -2;
    background:
        radial-gradient(circle at 8% 12%, rgba(168,137,255,.2), transparent 25rem),
        radial-gradient(circle at 88% 82%, rgba(107,231,220,.12), transparent 26rem),
        linear-gradient(140deg, #090a10, #0d0f18);
}

.app {
    display: flex;
    min-height: 100vh;
}

.sidebar {
    width: 235px;
    flex: none;
    padding: 22px 16px;
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--line);
    background: rgba(8,9,14,.72);
    backdrop-filter: blur(18px);
}

.brand {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 4px 8px 26px;
}

.brand-mark {
    width: 40px;
    height: 40px;
    display: grid;
    place-items: center;
    border-radius: 13px;
    color: #10111a;
    font-weight: 900;
    background: linear-gradient(135deg, var(--purple), var(--cyan));
    box-shadow: 0 8px 24px rgba(168,137,255,.3);
}

.brand-name {
    font-weight: 800;
    letter-spacing: .14em;
    font-size: 15px;
}

.brand-sub {
    display: block;
    margin-top: 4px;
    color: var(--muted);
    font-size: 9px;
    letter-spacing: .16em;
}

.nav-label {
    padding: 8px 10px;
    color: #686b7d;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .16em;
}

.nav {
    display: grid;
    gap: 5px;
}

.nav button {
    height: 44px;
    display: flex;
    align-items: center;
    gap: 11px;
    border: 0;
    border-radius: 12px;
    padding: 0 12px;
    color: var(--muted);
    background: transparent;
    text-align: left;
}

.nav button:hover,
.nav button.active {
    color: var(--text);
    background: rgba(255,255,255,.06);
}

.nav button.active {
    box-shadow: inset 2px 0 var(--purple);
}

.nav-icon {
    width: 20px;
    color: var(--purple);
    text-align: center;
}

.sidebar-status {
    margin-top: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px;
    color: var(--muted);
    font-size: 12px;
}

.sidebar-status i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 12px var(--green);
}

.main {
    min-width: 0;
    flex: 1;
    overflow: auto;
}

.topbar {
    height: 72px;
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 30px;
    border-bottom: 1px solid var(--line);
    background: rgba(9,10,16,.72);
    backdrop-filter: blur(18px);
}

.topbar-title {
    font-size: 13px;
    font-weight: 700;
}

.topbar-right {
    display: flex;
    align-items: center;
    gap: 14px;
}

.clock {
    color: var(--muted);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
}

.avatar {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    color: white;
    font-weight: 900;
    background: linear-gradient(135deg, var(--purple), var(--pink));
}

.page {
    width: min(1220px, 100%);
    margin: auto;
    padding: 34px 32px 55px;
}

.view {
    display: none;
}

.view.active {
    display: block;
    animation: fadeIn .25s ease;
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}

.surface {
    border: 1px solid var(--line);
    border-radius: 20px;
    background: var(--panel);
    backdrop-filter: blur(14px);
}

.hero {
    position: relative;
    min-height: 280px;
    overflow: hidden;
    padding: 44px;
    background:
        linear-gradient(100deg, rgba(168,137,255,.16), transparent 60%),
        var(--panel);
}

.hero::after {
    content: "";
    position: absolute;
    width: 330px;
    height: 330px;
    top: -170px;
    right: -80px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--purple), var(--cyan));
    filter: blur(55px);
    opacity: .23;
}

.eyebrow {
    color: var(--cyan);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .2em;
}

.hero h1,
.page-title {
    margin: 17px 0 13px;
    font-size: clamp(38px, 5vw, 68px);
    line-height: .96;
    letter-spacing: -.05em;
}

.hero p {
    max-width: 550px;
    color: var(--muted);
    line-height: 1.6;
}

.actions,
.toolbar {
    display: flex;
    gap: 10px;
    margin-top: 24px;
}

.primary,
.secondary {
    min-height: 44px;
    border-radius: 12px;
    padding: 0 16px;
    font-weight: 800;
}

.primary {
    border: 0;
    color: #10111a;
    background: linear-gradient(135deg, var(--purple), var(--cyan));
}

.secondary {
    border: 1px solid var(--line);
    color: var(--text);
    background: rgba(255,255,255,.06);
}

.stat-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-top: 16px;
}

.stat {
    padding: 19px;
}

.stat strong {
    display: block;
    font-size: 26px;
}

.stat span,
.muted {
    color: var(--muted);
    font-size: 12px;
}

.page-title {
    font-size: 42px;
}

.search {
    width: 100%;
    min-height: 46px;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0 14px;
    outline: none;
    color: var(--text);
    background: rgba(255,255,255,.045);
}

.search:focus {
    border-color: var(--purple);
}

.games-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(235px, 1fr));
    gap: 14px;
    margin-top: 16px;
}

.game-card {
    padding: 17px;
    transition: .2s ease;
}

.game-card:hover {
    transform: translateY(-4px);
    border-color: rgba(168,137,255,.5);
}

.game-card-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.game-number {
    color: var(--purple);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .15em;
}

.favorite {
    border: 0;
    color: var(--muted);
    background: transparent;
    font-size: 19px;
}

.favorite.active {
    color: var(--pink);
}

.game-card h3 {
    min-height: 44px;
    margin: 16px 0 10px;
    font-size: 17px;
}

.game-meta {
    display: flex;
    justify-content: space-between;
    margin-bottom: 13px;
    color: var(--muted);
    font-size: 11px;
}

.launch-button {
    width: 100%;
    height: 42px;
    border: 0;
    border-radius: 11px;
    color: #10111a;
    font-weight: 800;
    background: linear-gradient(135deg, var(--purple), var(--cyan));
}

.loader {
    display: none;
    margin: 17px 0;
    padding: 17px;
}

.loader.active {
    display: block;
}

.loader-head,
.loader-foot {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    color: var(--muted);
    font-size: 12px;
}

.loader-head {
    margin-bottom: 11px;
    color: var(--text);
    font-weight: 700;
}

.progress-track {
    height: 9px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(255,255,255,.08);
}

.progress-fill {
    width: 0%;
    height: 100%;
    transition: width .15s ease;
    background: linear-gradient(90deg, var(--purple), var(--cyan));
}

.table {
    width: 100%;
    border-collapse: collapse;
}

.table th,
.table td {
    padding: 13px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    font-size: 13px;
}

.table th {
    color: var(--muted);
    font-size: 10px;
    letter-spacing: .12em;
    text-transform: uppercase;
}

.info-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 13px;
}

.info {
    padding: 19px;
}

.info strong {
    display: block;
    margin-top: 7px;
    font-size: 18px;
}

.empty {
    padding: 48px 20px;
    color: var(--muted);
    text-align: center;
}

.toast {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 100;
    max-width: 340px;
    padding: 13px 15px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--panel-2);
    box-shadow: 0 14px 40px rgba(0,0,0,.4);
}

@media (max-width: 850px) {
    .sidebar { width: 78px; }
    .brand-name,
    .nav-label,
    .nav button span:last-child,
    .sidebar-status { display: none; }
    .nav button { justify-content: center; }
    .page { padding: 24px 18px 45px; }
    .topbar { padding: 0 18px; }
}

@media (max-width: 560px) {
    .app { display: block; }
    .sidebar {
        position: fixed;
        right: 0;
        bottom: 0;
        left: 0;
        z-index: 50;
        width: 100%;
        height: 66px;
        padding: 8px 12px;
        border-top: 1px solid var(--line);
        border-right: 0;
    }
    .nav {
        width: 100%;
        grid-template-columns: repeat(3, 1fr);
    }
    .nav button { height: 50px; }
    .main { padding-bottom: 66px; }
    .topbar { height: 58px; }
    .hero { padding: 28px; }
    .hero h1 { font-size: 40px; }
    .actions,
    .toolbar { flex-direction: column; }
    .stat-grid,
    .info-grid { grid-template-columns: 1fr; }
    .games-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="background"></div>
<canvas id="particles"></canvas>

<div class="app">
    <aside class="sidebar">
        <div class="brand">
            <div class="brand-mark">X</div>
            <div class="brand-name">
                CODEX
                <span class="brand-sub">RETURNS ARCADE</span>
            </div>
        </div>

        <div class="nav-label">NAVIGATION</div>

        <nav class="nav">
            <button class="active" data-view="home">
                <span class="nav-icon">⌂</span>
                <span>Home</span>
            </button>
            <button data-view="games">
                <span class="nav-icon">▦</span>
                <span>Games</span>
            </button>
            <button data-view="stats">
                <span class="nav-icon">◒</span>
                <span>Stats</span>
            </button>
        </nav>

        <div class="sidebar-status">
            <i></i>
            <span>LOCAL ENGINE ONLINE</span>
        </div>
    </aside>

    <section class="main">
        <header class="topbar">
            <div id="topbar-title" class="topbar-title">Welcome back</div>
            <div class="topbar-right">
                <span id="clock" class="clock">00:00:00</span>
                <span id="battery" class="clock">Battery --</span>
                <span class="avatar">X</span>
            </div>
        </header>

        <main class="page">
            <section id="view-home" class="view active">
                <div class="hero surface">
                    <div class="eyebrow">NO LOGIN · LOCALHOST ARCADE</div>
                    <h1>Play something<br>different.</h1>
                    <p>
                        Browse your connected game library, cache games locally,
                        and launch them instantly through the local engine.
                    </p>
                    <div class="actions">
                        <button class="primary" onclick="showView('games')">
                            BROWSE GAMES
                        </button>
                        <button class="secondary" onclick="syncGames()">
                            SYNC LIBRARY
                        </button>
                    </div>
                </div>

                <div class="stat-grid">
                    <div class="surface stat">
                        <strong id="home-game-count">0</strong>
                        <span>Games available</span>
                    </div>
                    <div class="surface stat">
                        <strong id="home-favorite-count">0</strong>
                        <span>Favorites</span>
                    </div>
                    <div class="surface stat">
                        <strong id="home-port-count">0</strong>
                        <span>Active ports</span>
                    </div>
                </div>

                <div class="surface" style="margin-top:16px;padding:20px">
                    <div class="muted">CURRENT QUOTE</div>
                    <h3 id="quote-text">Loading quote...</h3>
                    <div id="quote-author" class="muted"></div>
                    <button class="secondary" style="margin-top:14px" onclick="nextQuote()">
                        NEXT QUOTE
                    </button>
                </div>
            </section>

            <section id="view-games" class="view">
                <div class="eyebrow">LIBRARY</div>
                <h1 class="page-title">Your games</h1>

                <div class="toolbar">
                    <input
                        id="search"
                        class="search"
                        placeholder="Search by game name or number..."
                        oninput="renderGames()"
                    >
                    <button class="secondary" onclick="syncGames()">SYNC</button>
                </div>

                <div id="loader" class="surface loader">
                    <div class="loader-head">
                        <span id="loader-stage">Preparing game...</span>
                        <span id="loader-percent">0%</span>
                    </div>
                    <div class="progress-track">
                        <div id="loader-fill" class="progress-fill"></div>
                    </div>
                    <div class="loader-foot" style="margin-top:10px">
                        <span id="loader-detail">Waiting...</span>
                        <span id="loader-files">0 / 0</span>
                    </div>
                </div>

                <div id="games-grid" class="games-grid"></div>
            </section>

            <section id="view-stats" class="view">
                <div class="eyebrow">OVERVIEW</div>
                <h1 class="page-title">Your stats</h1>

                <div class="info-grid">
                    <div class="surface info">
                        <div class="muted">TOTAL GAMES</div>
                        <strong id="stats-games">0</strong>
                    </div>
                    <div class="surface info">
                        <div class="muted">CACHED GAMES</div>
                        <strong id="stats-cached">0</strong>
                    </div>
                    <div class="surface info">
                        <div class="muted">FAVORITES</div>
                        <strong id="stats-favorites">0</strong>
                    </div>
                    <div class="surface info">
                        <div class="muted">ACTIVE PORTS</div>
                        <strong id="stats-ports">0</strong>
                    </div>
                </div>

                <div class="surface" style="margin-top:16px;padding:20px">
                    <h3>Active game ports</h3>
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Game</th>
                                <th>Port</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="ports-table"></tbody>
                    </table>
                </div>
            </section>
        </main>
    </section>
</div>

<div id="toast" class="toast" hidden></div>

<script>
let games = [];
let ports = [];
let favorites = new Set(
    JSON.parse(localStorage.getItem("codex-favorites") || "[]")
);
let busy = false;
let progressTimer = null;

const $ = id => document.getElementById(id);

function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => {
        toast.hidden = true;
    }, 3000);
}

function showView(name) {
    document.querySelectorAll(".view").forEach(view => {
        view.classList.toggle("active", view.id === `view-${name}`);
    });

    document.querySelectorAll(".nav button").forEach(button => {
        button.classList.toggle("active", button.dataset.view === name);
    });

    $("topbar-title").textContent =
        name === "home" ? "Welcome back" :
        name === "games" ? "Game library" :
        "Launcher stats";

    if (name === "stats") renderStats();
}

document.querySelectorAll(".nav button").forEach(button => {
    button.addEventListener("click", () => showView(button.dataset.view));
});

function updateClock() {
    $("clock").textContent = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
    });
}

setInterval(updateClock, 1000);
updateClock();

if (navigator.getBattery) {
    navigator.getBattery().then(battery => {
        const updateBattery = () => {
            $("battery").textContent =
                `Battery ${Math.round(battery.level * 100)}%`;
        };
        updateBattery();
        battery.addEventListener("levelchange", updateBattery);
    });
}

function saveFavorites() {
    localStorage.setItem(
        "codex-favorites",
        JSON.stringify([...favorites])
    );
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function updateCounters() {
    const cached = games.filter(game => game.cached).length;
    const favoriteCount = games.filter(game =>
        favorites.has(String(game.num))
    ).length;

    $("home-game-count").textContent = games.length;
    $("home-favorite-count").textContent = favoriteCount;
    $("home-port-count").textContent = ports.length;

    $("stats-games").textContent = games.length;
    $("stats-cached").textContent = cached;
    $("stats-favorites").textContent = favoriteCount;
    $("stats-ports").textContent = ports.length;
}

function renderGames() {
    const query = $("search").value.trim().toLowerCase();

    const filtered = games.filter(game =>
        !query ||
        game.name.toLowerCase().includes(query) ||
        String(game.num) === query
    );

    if (!filtered.length) {
        $("games-grid").innerHTML =
            '<div class="surface empty">No games found.</div>';
        updateCounters();
        return;
    }

    $("games-grid").innerHTML = filtered.map(game => {
        const isFavorite = favorites.has(String(game.num));

        return `
            <article class="surface game-card">
                <div class="game-card-top">
                    <span class="game-number">GAME #${game.num}</span>
                    <button
                        class="favorite ${isFavorite ? "active" : ""}"
                        onclick="toggleFavorite(${game.num})"
                        title="Favorite game"
                    >${isFavorite ? "★" : "☆"}</button>
                </div>
                <h3>${escapeHtml(game.name)}</h3>
                <div class="game-meta">
                    <span>${game.file_count || 0} files</span>
                    <span>${escapeHtml(game.size_formatted || "0 B")}</span>
                </div>
                <button
                    class="launch-button"
                    onclick="launchGame(${game.num})"
                    ${busy ? "disabled" : ""}
                >LAUNCH</button>
            </article>
        `;
    }).join("");

    updateCounters();
}

function toggleFavorite(number) {
    const key = String(number);
    if (favorites.has(key)) favorites.delete(key);
    else favorites.add(key);
    saveFavorites();
    renderGames();
    renderStats();
}

function renderStats() {
    updateCounters();

    $("ports-table").innerHTML = ports.length
        ? ports.map(port => `
            <tr>
                <td>#${port.num} ${escapeHtml(port.name)}</td>
                <td>${port.port}</td>
                <td>
                    <button
                        class="secondary"
                        onclick="openPort('${escapeHtml(port.url)}')"
                    >OPEN</button>
                    <button
                        class="secondary"
                        onclick="stopPort(${port.port})"
                    >STOP</button>
                </td>
            </tr>
        `).join("")
        : '<tr><td colspan="3" class="muted">No active ports.</td></tr>';
}

function openPort(url) {
    window.open(url, "_blank");
}

function renderQuote(quote) {
    if (!quote) return;
    $("quote-text").textContent = (quote.lines || []).join(" ");
    $("quote-author").textContent = `— ${quote.author || "CODEX"}`;
}

async function nextQuote() {
    try {
        const response = await fetch("/api/quote");
        const data = await response.json();
        renderQuote(data.quote);
    } catch (error) {
        showToast("Could not load a new quote.");
    }
}

async function loadGames() {
    try {
        const response = await fetch("/api/games");
        const data = await response.json();

        games = data.games || [];
        ports = data.active_ports || [];
        renderQuote(data.quote);
        renderGames();
        renderStats();

        if (data.status) showToast(data.status);
    } catch (error) {
        showToast(`Could not load games: ${error}`);
    }
}

async function syncGames() {
    if (busy) return;

    busy = true;
    renderGames();
    showToast("Syncing game library...");

    try {
        const response = await fetch("/api/refresh", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: "{}"
        });
        const data = await response.json();

        games = data.games || [];
        ports = data.active_ports || [];
        renderGames();
        renderStats();
        showToast(data.status || "Library synchronized.");
    } catch (error) {
        showToast(`Sync failed: ${error}`);
    } finally {
        busy = false;
        renderGames();
    }
}

function updateProgress(data) {
    const percent = Math.max(
        0,
        Math.min(100, Number(data.percent || 0))
    );

    $("loader-stage").textContent = data.stage || "Working...";
    $("loader-percent").textContent = `${percent.toFixed(1)}%`;
    $("loader-fill").style.width = `${percent}%`;
    $("loader-detail").textContent = data.detail || "";
    $("loader-files").textContent =
        `${data.filesDone || 0} / ${data.filesTotal || 0}`;
}

async function launchGame(number) {
    if (busy) return;

    busy = true;
    showView("games");
    $("loader").classList.add("active");
    updateProgress({
        stage: "Preparing",
        detail: `Starting Game #${number}...`,
        percent: 1
    });
    renderGames();

    progressTimer = setInterval(async () => {
        try {
            const response = await fetch("/api/progress");
            updateProgress(await response.json());
        } catch (_) {}
    }, 150);

    try {
        const response = await fetch("/api/launch", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                num: number,
                force_redownload: false
            })
        });
        const data = await response.json();

        games = data.games || games;
        ports = data.active_ports || ports;

        if (data.progress) updateProgress(data.progress);
        renderGames();
        renderStats();

        if (data.ok && data.url) {
            showToast(`Game ready at ${data.url}`);
            window.open(data.url, "_blank");
        } else {
            showToast(data.status || "Game launch failed.");
        }
    } catch (error) {
        showToast(`Launch failed: ${error}`);
    } finally {
        clearInterval(progressTimer);
        progressTimer = null;
        busy = false;
        renderGames();
    }
}

async function stopPort(port) {
    try {
        const response = await fetch("/api/stop_port", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({port})
        });
        const data = await response.json();

        games = data.games || games;
        ports = data.active_ports || [];
        renderGames();
        renderStats();
        showToast(data.status || "Port stopped.");
    } catch (error) {
        showToast(`Could not stop port: ${error}`);
    }
}

const canvas = $("particles");
const context = canvas.getContext("2d");
let particleList = [];

function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
}

function createParticles() {
    particleList = Array.from(
        {length: Math.min(80, Math.floor(window.innerWidth / 16))},
        () => ({
            x: Math.random() * canvas.width,
            y: Math.random() * canvas.height,
            radius: Math.random() * 1.8 + .4,
            speed: Math.random() * .35 + .1,
            alpha: Math.random() * .55 + .1
        })
    );
}

function animateParticles() {
    context.clearRect(0, 0, canvas.width, canvas.height);

    for (const particle of particleList) {
        particle.y -= particle.speed;
        if (particle.y < -5) {
            particle.y = canvas.height + 5;
            particle.x = Math.random() * canvas.width;
        }

        context.fillStyle = `rgba(168,137,255,${particle.alpha})`;
        context.beginPath();
        context.arc(
            particle.x,
            particle.y,
            particle.radius,
            0,
            Math.PI * 2
        );
        context.fill();
    }

    requestAnimationFrame(animateParticles);
}

window.addEventListener("resize", () => {
    resizeCanvas();
    createParticles();
});

resizeCanvas();
createParticles();
animateParticles();
loadGames();
</script>
</body>
</html>
"""


# =============================================================================
# Local launcher API
# =============================================================================

def make_ui_handler(state: LauncherState):
    class UIHandler(QuietUIHandler):
        def send_json(self, payload: dict, status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlparse(self.path).path

            if path in {"/", "/index.html"}:
                raw = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if path == "/api/games":
                with state.lock:
                    payload = {
                        "games": state.serialize_games(),
                        "active_ports": state.active_ports(),
                        "status": state.status,
                        "quote": dict(state.quote),
                    }
                self.send_json(payload)
                return

            if path == "/api/quote":
                self.send_json({"quote": state.next_quote()})
                return

            if path == "/api/progress":
                self.send_json(state.get_progress())
                return

            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length).decode("utf-8")

            try:
                body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                body = {}

            if path == "/api/refresh":
                self.send_json(state.refresh_games())
                return

            if path == "/api/launch":
                try:
                    number = int(body.get("num", 0))
                    force = bool(body.get("force_redownload", False))
                    self.send_json(state.launch_game(number, force))
                except (TypeError, ValueError) as error:
                    self.send_json({"ok": False, "status": str(error)}, 400)
                return

            if path == "/api/stop_port":
                try:
                    self.send_json(state.stop_port(int(body.get("port", 0))))
                except (TypeError, ValueError) as error:
                    self.send_json({"ok": False, "status": str(error)}, 400)
                return

            self.send_error(404)

    return UIHandler

# =============================================================================
# Application entry point
# =============================================================================

if __name__ == "__main__":
    hide_console_window()

    state = LauncherState()

    ui_port, _ = find_free_port()
    ui_server = ThreadingHTTPServer(
        ("127.0.0.1", ui_port),
        make_ui_handler(state),
    )

    threading.Thread(
        target=ui_server.serve_forever,
        daemon=True,
    ).start()

    window = webview.create_window(
        title="CodeX Returns Arcade",
        url=f"http://127.0.0.1:{ui_port}/",
        width=1366,
        height=820,
        min_size=(860, 600),
        maximized=True,
        background_color="#090a10",
    )

    try:
        webview.start(
            func=lambda: maximize_app_window(window),
            debug=False,
        )
    finally:
        state.stop_all()
        ui_server.shutdown()
        ui_server.server_close()
