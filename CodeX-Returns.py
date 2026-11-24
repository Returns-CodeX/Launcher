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

# =============================================================================
# Black-and-red launcher interface
# =============================================================================

HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090707">
<title>CODEX-RETURNS // ARCADE</title>
<style>
:root {
    color-scheme: dark;
    --bg: #090707;
    --bg-soft: #100a0b;
    --panel: rgba(20, 12, 13, .88);
    --panel-2: #170d0f;
    --line: rgba(255, 49, 67, .17);
    --line-hot: rgba(255, 49, 67, .42);
    --red: #ff263d;
    --red-hot: #ff4657;
    --red-dim: #a50f23;
    --text: #f7f1f1;
    --muted: #a59698;
    --green: #63e6a5;
    --gold: #d7b16a;
    --radius: 16px;
    --glow: 0 0 28px rgba(255, 38, 61, .14);
}
* { box-sizing: border-box; }
html, body { min-height: 100%; margin: 0; }
body {
    overflow: hidden;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", Arial, sans-serif;
}
button, input, select { font: inherit; }
button { cursor: pointer; }
button:disabled { opacity: .48; cursor: not-allowed; }
button:focus-visible, input:focus-visible, select:focus-visible {
    outline: 2px solid var(--red-hot);
    outline-offset: 3px;
}
#fx-canvas {
    position: fixed;
    inset: 0;
    z-index: -1;
    width: 100%;
    height: 100%;
    pointer-events: none;
}
.backdrop {
    position: fixed;
    inset: 0;
    z-index: -2;
    pointer-events: none;
    background:
        radial-gradient(ellipse at 12% 8%, rgba(128, 12, 27, .23), transparent 34rem),
        radial-gradient(ellipse at 92% 92%, rgba(89, 8, 18, .19), transparent 33rem),
        linear-gradient(145deg, #090707 0%, #0c0809 58%, #080707 100%);
}
.scanlines {
    position: fixed;
    inset: 0;
    z-index: 15;
    pointer-events: none;
    opacity: .12;
    background: repeating-linear-gradient(
        to bottom, transparent 0 3px, rgba(255,255,255,.025) 4px
    );
}
.app { display: flex; width: 100%; height: 100vh; }
.sidebar {
    width: 238px;
    flex: none;
    display: flex;
    flex-direction: column;
    padding: 20px 14px;
    border-right: 1px solid var(--line);
    background: rgba(9, 7, 7, .85);
    backdrop-filter: blur(18px);
}
.brand {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 5px 8px 26px;
}
.brand-mark {
    width: 42px;
    height: 42px;
    display: grid;
    place-items: center;
    border: 1px solid rgba(255,255,255,.18);
    border-radius: 12px;
    color: white;
    font-weight: 1000;
    font-size: 19px;
    background: linear-gradient(145deg, #ff3b4e, #8e071a);
    box-shadow: 0 0 24px rgba(255,38,61,.3), inset 0 1px rgba(255,255,255,.25);
}
.brand-name {
    font-size: 13px;
    font-weight: 900;
    letter-spacing: .16em;
}
.brand-sub {
    display: block;
    margin-top: 4px;
    color: var(--muted);
    font-size: 9px;
    letter-spacing: .17em;
}
.nav-label {
    padding: 7px 10px;
    color: #806f71;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: .18em;
}
.nav { display: grid; gap: 5px; }
.nav button {
    display: flex;
    align-items: center;
    gap: 12px;
    min-height: 43px;
    padding: 0 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    color: #b7a9aa;
    background: transparent;
    text-align: left;
    transition: .18s ease;
}
.nav button:hover {
    color: white;
    border-color: var(--line);
    background: rgba(255,38,61,.055);
}
.nav button.active {
    color: white;
    border-color: rgba(255,38,61,.24);
    background: linear-gradient(90deg, rgba(255,38,61,.16), rgba(255,38,61,.025));
    box-shadow: inset 2px 0 var(--red), var(--glow);
}
.nav-icon { width: 20px; color: var(--red-hot); text-align: center; }
.nav-count {
    min-width: 22px;
    margin-left: auto;
    padding: 2px 6px;
    border-radius: 20px;
    color: #ffd9dc;
    background: rgba(255,38,61,.15);
    font-size: 10px;
    text-align: center;
}
.sidebar-bottom { margin-top: auto; }
.engine-card {
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(255,38,61,.035);
}
.engine-top { display: flex; align-items: center; gap: 8px; font-size: 11px; }
.engine-led {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green); box-shadow: 0 0 9px var(--green);
}
.engine-detail { margin: 7px 0 0 15px; color: var(--muted); font-size: 10px; }
.main { min-width: 0; flex: 1; overflow: auto; }
.topbar {
    height: 66px;
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    border-bottom: 1px solid var(--line);
    background: rgba(9,7,7,.78);
    backdrop-filter: blur(18px);
}
.breadcrumb { color: var(--muted); font-size: 12px; }
.breadcrumb strong { color: var(--text); font-weight: 700; }
.top-actions { display: flex; align-items: center; gap: 12px; }
.clock { color: var(--muted); font: 11px Consolas, monospace; }
.top-indicator {
    display: flex; align-items: center; gap: 7px;
    color: var(--muted); font-size: 10px; letter-spacing: .06em;
}
.top-indicator .engine-led { width: 6px; height: 6px; }
.icon-button {
    width: 34px; height: 34px;
    border: 1px solid var(--line); border-radius: 9px;
    color: var(--muted); background: rgba(255,255,255,.025);
}
.icon-button:hover { color: white; border-color: var(--line-hot); }
.page { width: min(1280px, 100%); margin: auto; padding: 30px 30px 54px; }
.view { display: none; }
.view.active { display: block; animation: appear .24s ease-out; }
@keyframes appear { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: none; } }
.eyebrow {
    color: var(--red-hot); font-size: 9px; font-weight: 900;
    letter-spacing: .2em; text-transform: uppercase;
}
.section-heading {
    display: flex; align-items: end; justify-content: space-between;
    gap: 16px; margin: 0 0 18px;
}
.page-title {
    margin: 7px 0 0;
    font-size: clamp(29px, 4vw, 43px);
    line-height: 1;
    letter-spacing: -.04em;
}
.muted { color: var(--muted); }
.panel {
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--panel);
    box-shadow: 0 14px 40px rgba(0,0,0,.2);
    backdrop-filter: blur(13px);
}
.hero {
    position: relative;
    min-height: 278px;
    display: flex;
    align-items: center;
    overflow: hidden;
    padding: 38px;
    background:
        linear-gradient(100deg, rgba(23,9,12,.98) 0%, rgba(19,10,12,.9) 54%, rgba(34,7,13,.72) 100%);
}
.hero::before {
    content: "";
    position: absolute;
    top: 0; right: 0; bottom: 0;
    width: 42%;
    opacity: .58;
    background:
        linear-gradient(135deg, transparent 0 43%, rgba(255,38,61,.07) 43% 44%, transparent 44%),
        repeating-linear-gradient(135deg, transparent 0 19px, rgba(255,38,61,.05) 20px 21px);
    mask-image: linear-gradient(90deg, transparent, black);
}
.hero::after {
    content: "";
    position: absolute;
    top: -150px; right: 4%;
    width: 310px; height: 310px;
    border: 1px solid rgba(255,49,67,.28);
    border-radius: 50%;
    box-shadow: 0 0 0 24px rgba(255,49,67,.025), 0 0 0 50px rgba(255,49,67,.02);
}
.hero-content { position: relative; z-index: 1; max-width: 650px; }
.hero h1 { margin: 14px 0 12px; font-size: clamp(42px, 6vw, 72px); line-height: .9; letter-spacing: -.06em; }
.hero h1 span { color: var(--red); text-shadow: 0 0 30px rgba(255,38,61,.28); }
.hero p { max-width: 520px; color: #b6a8aa; font-size: 13px; line-height: 1.7; }
.button-row { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 22px; }
.btn {
    min-height: 40px;
    padding: 0 14px;
    border: 1px solid var(--line-hot);
    border-radius: 9px;
    color: white;
    background: rgba(255,38,61,.08);
    font-size: 10px;
    font-weight: 900;
    letter-spacing: .08em;
    transition: .18s ease;
}
.btn:hover { border-color: var(--red-hot); background: rgba(255,38,61,.16); transform: translateY(-1px); }
.btn.primary {
    border-color: var(--red);
    background: linear-gradient(135deg, #ee1e35, #a90d22);
    box-shadow: 0 6px 20px rgba(255,38,61,.2);
}
.btn.quiet { color: var(--muted); border-color: var(--line); background: rgba(255,255,255,.025); }
.btn.small { min-height: 31px; padding: 0 9px; font-size: 9px; }
.metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0,1fr));
    gap: 12px;
    margin-top: 14px;
}
.metric { position: relative; padding: 16px; overflow: hidden; }
.metric::after {
    content: ""; position: absolute; top: 0; left: 16px; width: 28px; height: 2px;
    background: var(--red); box-shadow: 0 0 10px var(--red);
}
.metric-label { color: var(--muted); font-size: 9px; font-weight: 800; letter-spacing: .12em; }
.metric-value { margin-top: 8px; font-size: 25px; font-weight: 800; }
.metric-note { margin-top: 3px; color: #806f71; font-size: 10px; }
.home-columns {
    display: grid; grid-template-columns: minmax(0,1.4fr) minmax(260px,.8fr);
    gap: 14px; margin-top: 14px;
}
.block { padding: 18px; }
.block-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.block-heading h2 { margin: 0; font-size: 13px; letter-spacing: .02em; }
.text-link { border: 0; color: var(--red-hot); background: none; font-size: 10px; font-weight: 800; }
.recent-list { display: grid; gap: 8px; }
.recent-item, .port-item {
    display: flex; align-items: center; gap: 12px; min-width: 0;
    padding: 10px; border: 1px solid rgba(255,255,255,.045); border-radius: 10px;
    background: rgba(255,255,255,.018);
}
.game-symbol {
    width: 35px; height: 35px; flex: none; display: grid; place-items: center;
    border: 1px solid var(--line); border-radius: 9px; color: var(--red-hot);
    background: rgba(255,38,61,.07); font-size: 11px; font-weight: 900;
}
.recent-copy { min-width: 0; flex: 1; }
.recent-copy strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.recent-copy span { display: block; margin-top: 4px; color: var(--muted); font-size: 9px; }
.quote-text { color: #e8dfe0; font-size: 15px; line-height: 1.6; }
.quote-author { margin-top: 12px; color: var(--gold); font-size: 9px; font-weight: 900; letter-spacing: .15em; }
.quote-mark { color: var(--red); font-size: 27px; line-height: 1; }
.toolbar { display: flex; gap: 9px; margin: 18px 0 14px; }
.search-wrap { position: relative; flex: 1; }
.search-icon { position: absolute; top: 12px; left: 13px; color: #806f71; font-size: 13px; }
.search, .select {
    width: 100%; height: 40px;
    border: 1px solid var(--line); border-radius: 9px;
    color: var(--text); background: rgba(255,255,255,.035);
}
.search { padding: 0 12px 0 35px; }
.search::placeholder { color: #786d6e; }
.select { width: auto; min-width: 125px; padding: 0 10px; }
.game-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(215px,1fr)); gap: 12px; }
.game-card {
    position: relative; min-width: 0; overflow: hidden; padding: 15px;
    transition: .2s ease;
}
.game-card:hover { transform: translateY(-3px); border-color: var(--line-hot); box-shadow: var(--glow); }
.game-card::before {
    content: ""; position: absolute; top: 0; left: 0; width: 34px; height: 1px;
    background: var(--red); box-shadow: 0 0 10px var(--red);
}
.game-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.game-number { color: var(--red-hot); font: 10px Consolas,monospace; letter-spacing: .08em; }
.favorite {
    width: 28px; height: 28px; border: 1px solid transparent; border-radius: 7px;
    color: #8c7c7e; background: transparent; font-size: 17px;
}
.favorite:hover, .favorite.on { color: var(--red-hot); border-color: var(--line); background: rgba(255,38,61,.07); }
.game-card h3 {
    min-height: 38px; margin: 13px 0 10px; overflow-wrap: anywhere;
    font-size: 14px; line-height: 1.35;
}
.game-meta { display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 9px; }
.cache-tag { color: var(--green); font-weight: 800; }
.game-card .launch-button { width: 100%; margin-top: 14px; }
.loader { display: none; margin: 14px 0; padding: 14px; }
.loader.active { display: block; }
.loader-top, .loader-bottom { display: flex; justify-content: space-between; gap: 10px; font-size: 10px; }
.loader-top { margin-bottom: 9px; }
.loader-bottom { margin-top: 8px; color: var(--muted); }
.track { height: 7px; overflow: hidden; border-radius: 8px; background: rgba(255,255,255,.08); }
.fill { width: 0; height: 100%; background: linear-gradient(90deg,var(--red-dim),var(--red-hot)); box-shadow: 0 0 12px rgba(255,38,61,.5); transition: width .16s; }
.empty { padding: 38px 20px; color: var(--muted); text-align: center; }
.stats-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }
.table-wrap { overflow-x: auto; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: 11px 9px; border-bottom: 1px solid var(--line); text-align: left; font-size: 10px; }
.table th { color: #8d7b7d; font-size: 8px; letter-spacing: .12em; }
.table tr:last-child td { border-bottom: 0; }
.setting-row {
    display: flex; align-items: center; justify-content: space-between; gap: 18px;
    padding: 14px 0; border-bottom: 1px solid var(--line);
}
.setting-row:last-child { border-bottom: 0; }
.setting-row strong { display: block; font-size: 11px; }
.setting-row span { display: block; margin-top: 4px; color: var(--muted); font-size: 10px; }
.switch { accent-color: var(--red); width: 17px; height: 17px; }
.toast {
    position: fixed; right: 18px; bottom: 18px; z-index: 30;
    max-width: min(390px,calc(100vw - 36px)); padding: 12px 14px;
    border: 1px solid var(--line-hot); border-radius: 10px;
    background: #180c0e; box-shadow: 0 12px 34px rgba(0,0,0,.5);
    font-size: 11px;
}
[hidden] { display: none !important; }
@media(max-width:900px) {
    .sidebar { width: 70px; padding: 16px 9px; }
    .brand { justify-content: center; padding: 5px 0 24px; }
    .brand-name,.nav-label,.nav button .nav-text,.nav-count,.sidebar-bottom { display: none; }
    .nav button { justify-content: center; padding: 0; }
    .home-columns { grid-template-columns: 1fr; }
    .page { padding: 24px 18px 44px; }
    .topbar { padding: 0 18px; }
}
@media(max-width:600px) {
    .app { display: block; }
    .sidebar {
        position: fixed; right: 0; bottom: 0; left: 0; z-index: 20;
        width: 100%; height: 62px; padding: 5px 8px;
        border-top: 1px solid var(--line); border-right: 0;
    }
    .brand,.nav-label,.sidebar-bottom { display: none; }
    .nav { width: 100%; grid-template-columns: repeat(5,1fr); gap: 3px; }
    .nav button { min-height: 50px; border-radius: 8px; }
    .nav-icon { width: auto; font-size: 15px; }
    .nav button .nav-text { display: block; font-size: 8px; }
    .nav button { flex-direction: column; justify-content: center; gap: 2px; }
    .main { height: 100vh; padding-bottom: 62px; }
    .topbar { height: 54px; padding: 0 13px; }
    .top-indicator { display: none; }
    .page { padding: 20px 13px 82px; }
    .hero { min-height: 270px; padding: 25px 20px; }
    .hero::after { right: -190px; }
    .hero h1 { font-size: 48px; }
    .metrics { grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; }
    .metric { padding: 13px; }
    .metric-value { font-size: 21px; }
    .toolbar { flex-wrap: wrap; }
    .search-wrap { flex-basis: 100%; }
    .select { flex: 1; }
    .game-grid { grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; }
    .game-card { padding: 12px; }
    .game-card h3 { font-size: 12px; }
    .stats-grid { grid-template-columns: 1fr; }
}
@media(max-width:380px) {
    .game-grid { grid-template-columns: 1fr; }
    .clock { display: none; }
}
@media(prefers-reduced-motion:reduce) {
    *,*::before,*::after { scroll-behavior: auto !important; animation-duration: .01ms !important; transition-duration: .01ms !important; }
}
</style>
</head>
<body>
<div class="backdrop"></div>
<canvas id="fx-canvas" aria-hidden="true"></canvas>
<div class="scanlines"></div>

<div class="app">
    <aside class="sidebar">
        <div class="brand">
            <div class="brand-mark">X</div>
            <div class="brand-name">CODEX-RETURNS<span class="brand-sub">ARCADE CONSOLE</span></div>
        </div>

        <div class="nav-label">CONSOLE</div>
        <nav class="nav" aria-label="Main navigation">
            <button class="active" data-view="home"><span class="nav-icon">⌂</span><span class="nav-text">Home</span></button>
            <button data-view="games"><span class="nav-icon">▦</span><span class="nav-text">Library</span><span id="nav-game-count" class="nav-count">0</span></button>
            <button data-view="favorites"><span class="nav-icon">★</span><span class="nav-text">Favorites</span><span id="nav-fav-count" class="nav-count">0</span></button>
            <button data-view="stats"><span class="nav-icon">◒</span><span class="nav-text">Stats</span></button>
            <button data-view="settings"><span class="nav-icon">⚙</span><span class="nav-text">Settings</span></button>
        </nav>

        <div class="sidebar-bottom">
            <div class="engine-card">
                <div class="engine-top"><i class="engine-led"></i><strong>LOCAL ENGINE</strong></div>
                <div class="engine-detail">Ready · 127.0.0.1</div>
            </div>
        </div>
    </aside>

    <section class="main">
        <header class="topbar">
            <div class="breadcrumb">CODEX <span style="color:#60494c">/</span> <strong id="crumb">HOME</strong></div>
            <div class="top-actions">
                <span class="top-indicator"><i class="engine-led"></i> SYSTEM ONLINE</span>
                <span id="clock" class="clock">--:--:--</span>
                <button class="icon-button" title="Sync library" aria-label="Sync library" onclick="syncGames()">↻</button>
            </div>
        </header>

        <main class="page">
            <section id="view-home" class="view active">
                <div class="hero panel">
                    <div class="hero-content">
                        <div class="eyebrow">ALI'S PRODUCTIONS PRESENTS</div>
                        <h1>CODEX<br><span>RETURNS.</span></h1>
                        <p>Your local arcade, ready when you are. Browse the library, pick a game, and launch it through your own localhost game engine.</p>
                        <div class="button-row">
                            <button class="btn primary" onclick="showView('games')">▸ &nbsp; EXPLORE LIBRARY</button>
                            <button class="btn" onclick="syncGames()">↻ &nbsp; SYNC REPOSITORY</button>
                        </div>
                    </div>
                </div>

                <div class="metrics">
                    <div class="panel metric"><div class="metric-label">AVAILABLE GAMES</div><div id="home-games" class="metric-value">0</div><div class="metric-note">in your library</div></div>
                    <div class="panel metric"><div class="metric-label">CACHED LOCALLY</div><div id="home-cached" class="metric-value">0</div><div class="metric-note">ready for relaunch</div></div>
                    <div class="panel metric"><div class="metric-label">FAVORITES</div><div id="home-favorites" class="metric-value">0</div><div class="metric-note">your pinned games</div></div>
                    <div class="panel metric"><div class="metric-label">ACTIVE PORTS</div><div id="home-ports" class="metric-value">0</div><div class="metric-note">local game servers</div></div>
                </div>

                <div class="home-columns">
                    <section class="panel block">
                        <div class="block-heading"><h2>RECENTLY PLAYED</h2><button class="text-link" onclick="showView('games')">ALL GAMES →</button></div>
                        <div id="recent-list" class="recent-list"></div>
                    </section>
                    <section class="panel block">
                        <div class="block-heading"><h2>TRANSMISSION</h2><button class="text-link" onclick="nextQuote()">NEXT ↻</button></div>
                        <div class="quote-mark">“</div>
                        <div id="quote-text" class="quote-text">Loading transmission...</div>
                        <div id="quote-author" class="quote-author">CODEX</div>
                    </section>
                </div>
            </section>

            <section id="view-games" class="view">
                <div class="section-heading">
                    <div><div class="eyebrow">THE ARCADE</div><h1 class="page-title">Game library</h1></div>
                    <button class="btn" onclick="syncGames()">↻ &nbsp; SYNC</button>
                </div>
                <div class="toolbar">
                    <div class="search-wrap"><span class="search-icon">⌕</span><input id="game-search" class="search" placeholder="Search games by name or number..." oninput="renderGames()"></div>
                    <select id="game-sort" class="select" onchange="renderGames()">
                        <option value="number">Sort: game number</option>
                        <option value="name">Sort: name</option>
                        <option value="cached">Sort: cached first</option>
                    </select>
                    <button id="favorite-filter" class="btn quiet" onclick="toggleFavoriteFilter()">☆ FAVORITES</button>
                </div>
                <div id="loader" class="panel loader">
                    <div class="loader-top"><strong id="loader-stage">Preparing...</strong><span id="loader-percent">0%</span></div>
                    <div class="track"><div id="loader-fill" class="fill"></div></div>
                    <div class="loader-bottom"><span id="loader-detail">Waiting for engine...</span><span id="loader-files">0 / 0</span></div>
                </div>
                <div id="games-grid" class="game-grid"></div>
            </section>

            <section id="view-favorites" class="view">
                <div class="section-heading"><div><div class="eyebrow">YOUR SHORTLIST</div><h1 class="page-title">Favorites</h1></div></div>
                <div id="favorites-grid" class="game-grid"></div>
            </section>

            <section id="view-stats" class="view">
                <div class="section-heading"><div><div class="eyebrow">SYSTEM OVERVIEW</div><h1 class="page-title">Arcade stats</h1></div></div>
                <div class="stats-grid">
                    <div class="panel block"><div class="eyebrow">LIBRARY</div><div id="stats-library" class="metric-value">0</div><div class="muted">games indexed</div></div>
                    <div class="panel block"><div class="eyebrow">LOCAL CACHE</div><div id="stats-cache" class="metric-value">0</div><div class="muted">games with index.html cached</div></div>
                    <div class="panel block"><div class="eyebrow">FAVORITES</div><div id="stats-favs" class="metric-value">0</div><div class="muted">saved on this device</div></div>
                    <div class="panel block"><div class="eyebrow">ACTIVE SERVERS</div><div id="stats-ports" class="metric-value">0</div><div class="muted">localhost game ports</div></div>
                </div>
                <section class="panel block" style="margin-top:14px">
                    <div class="block-heading"><h2>ACTIVE GAME PORTS</h2></div>
                    <div class="table-wrap">
                        <table class="table"><thead><tr><th>Game</th><th>Address</th><th>Action</th></tr></thead><tbody id="ports-table"></tbody></table>
                    </div>
                </section>
            </section>

            <section id="view-settings" class="view">
                <div class="section-heading"><div><div class="eyebrow">PREFERENCES</div><h1 class="page-title">Settings</h1></div></div>
                <div class="panel block">
                    <div class="block-heading"><h2>VISUALS & INTERACTION</h2></div>
                    <div class="setting-row"><div><strong>Interactive background</strong><span>Particles react to pointer movement and clicks.</span></div><input id="setting-particles" class="switch" type="checkbox" checked></div>
                    <div class="setting-row"><div><strong>Reduced motion</strong><span>Disable motion effects for a calmer interface.</span></div><input id="setting-motion" class="switch" type="checkbox"></div>
                    <div class="setting-row"><div><strong>Background intensity</strong><span>Adjust particle density and glow.</span></div><input id="setting-intensity" type="range" min="0" max="100" value="65"></div>
                </div>
                <div class="panel block" style="margin-top:14px">
                    <div class="block-heading"><h2>LOCAL INSTALLATION</h2></div>
                    <div class="setting-row"><div><strong>Cache directory</strong><span>Downloaded game files are stored locally.</span></div><code>.cache/games/</code></div>
                    <div class="setting-row"><div><strong>Sign-in</strong><span>This launcher does not require an account.</span></div><strong style="color:var(--green)">NOT REQUIRED</strong></div>
                </div>
            </section>
        </main>
    </section>
</div>

<div id="toast" class="toast" hidden></div>

<script>
"use strict";

const $ = id => document.getElementById(id);
let games = [];
let ports = [];
let busy = false;
let progressTimer = null;
let favorites = new Set(readStored("codex-favorites", []).map(String));
let recent = readStored("codex-recent", []).map(Number).filter(Number.isFinite);
let favoritesOnly = false;
let quoteIndex = 0;

function readStored(key, fallback) {
    try {
        const value = JSON.parse(localStorage.getItem(key));
        return Array.isArray(value) ? value : fallback;
    } catch (_) {
        return fallback;
    }
}
function store(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
}
function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, char => ({
        "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
    })[char]);
}
function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3200);
}
function showView(name) {
    document.querySelectorAll(".view").forEach(view => {
        view.classList.toggle("active", view.id === `view-${name}`);
    });
    document.querySelectorAll(".nav button").forEach(button => {
        button.classList.toggle("active", button.dataset.view === name);
    });
    $("crumb").textContent = name.toUpperCase();
    if (name === "favorites") renderFavorites();
    if (name === "stats") renderStats();
    if (name === "home") renderHome();
}
document.querySelectorAll(".nav button").forEach(button => {
    button.addEventListener("click", () => showView(button.dataset.view));
});

function updateClock() {
    $("clock").textContent = new Date().toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", second: "2-digit"
    });
}
updateClock();
setInterval(updateClock, 1000);

function gameCard(game) {
    const key = String(game.num);
    const isFavorite = favorites.has(key);
    return `<article class="panel game-card">
        <div class="game-top">
            <span class="game-number">GAME // ${Number(game.num)}</span>
            <button class="favorite ${isFavorite ? "on" : ""}" aria-label="Toggle favorite"
                onclick="toggleFavorite(${Number(game.num)})">${isFavorite ? "★" : "☆"}</button>
        </div>
        <h3>${escapeHtml(game.name)}</h3>
        <div class="game-meta">
            <span>${Number(game.file_count || 0)} files · ${escapeHtml(game.size_formatted || "0 B")}</span>
            <span class="${game.cached ? "cache-tag" : ""}">${game.cached ? "CACHED" : "REMOTE"}</span>
        </div>
        <button class="btn primary launch-button" onclick="launchGame(${Number(game.num)})" ${busy ? "disabled" : ""}>▶ &nbsp; LAUNCH GAME</button>
    </article>`;
}
function renderGames() {
    const query = $("game-search").value.trim().toLowerCase();
    const sort = $("game-sort").value;
    let list = games.filter(game =>
        (!query || game.name.toLowerCase().includes(query) || String(game.num) === query) &&
        (!favoritesOnly || favorites.has(String(game.num)))
    );
    if (sort === "name") list.sort((a,b) => a.name.localeCompare(b.name));
    if (sort === "cached") list.sort((a,b) => Number(b.cached) - Number(a.cached));
    if (sort === "number") list.sort((a,b) => a.num - b.num);

    $("games-grid").innerHTML = list.length
        ? list.map(gameCard).join("")
        : `<div class="panel empty">${games.length ? "No games match this filter." : "Your library is empty. Sync the repository to load games."}</div>`;
    updateCounts();
}
function renderFavorites() {
    const list = games.filter(game => favorites.has(String(game.num)));
    $("favorites-grid").innerHTML = list.length
        ? list.map(gameCard).join("")
        : `<div class="panel empty">No favorites yet. Tap the star on a game to pin it here.</div>`;
}
function toggleFavorite(number) {
    const key = String(number);
    if (favorites.has(key)) favorites.delete(key); else favorites.add(key);
    store("codex-favorites", [...favorites]);
    renderGames();
    renderFavorites();
    renderHome();
    renderStats();
}
function toggleFavoriteFilter() {
    favoritesOnly = !favoritesOnly;
    $("favorite-filter").classList.toggle("primary", favoritesOnly);
    $("favorite-filter").textContent = favoritesOnly ? "★ FAVORITES ON" : "☆ FAVORITES";
    renderGames();
}
function updateCounts() {
    const cached = games.filter(game => game.cached).length;
    $("home-games").textContent = games.length;
    $("home-cached").textContent = cached;
    $("home-favorites").textContent = favorites.size;
    $("home-ports").textContent = ports.length;
    $("nav-game-count").textContent = games.length;
    $("nav-fav-count").textContent = favorites.size;
}
function renderHome() {
    updateCounts();
    const played = recent.map(number => games.find(game => game.num === number)).filter(Boolean).slice(0,4);
    $("recent-list").innerHTML = played.length ? played.map(game => `
        <div class="recent-item">
            <div class="game-symbol">#${game.num}</div>
            <div class="recent-copy"><strong>${escapeHtml(game.name)}</strong><span>${game.cached ? "Cached locally" : "Ready to fetch"} · Port ${game.port}</span></div>
            <button class="btn small" onclick="launchGame(${game.num})">PLAY</button>
        </div>`).join("") : `<div class="empty">Your recently played games will appear here.</div>`;
}
function renderStats() {
    $("stats-library").textContent = games.length;
    $("stats-cache").textContent = games.filter(game => game.cached).length;
    $("stats-favs").textContent = favorites.size;
    $("stats-ports").textContent = ports.length;
    $("ports-table").innerHTML = ports.length ? ports.map(port => `
        <tr><td>#${Number(port.num)} · ${escapeHtml(port.name)}</td>
        <td><code>${escapeHtml(port.url)}</code></td>
        <td><button class="btn small" onclick="stopPort(${Number(port.port)})">STOP</button></td></tr>
    `).join("") : `<tr><td colspan="3" class="muted">No game servers are running.</td></tr>`;
    updateCounts();
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
    } catch (_) { toast("Could not load a new transmission."); }
}
async function loadGames() {
    try {
        const response = await fetch("/api/games");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        games = data.games || [];
        ports = data.active_ports || [];
        renderQuote(data.quote);
        renderGames();
        renderFavorites();
        renderHome();
        renderStats();
    } catch (error) {
        toast(`Could not load the game library: ${error.message}`);
    }
}
async function syncGames() {
    if (busy) return;
    busy = true;
    renderGames();
    toast("Syncing game repository...");
    try {
        const response = await fetch("/api/refresh", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: "{}"
        });
        const data = await response.json();
        games = data.games || [];
        ports = data.active_ports || [];
        renderGames();
        renderFavorites();
        renderHome();
        renderStats();
        toast(data.status || "Library sync complete.");
    } catch (error) {
        toast(`Sync failed: ${error.message}`);
    } finally {
        busy = false;
        renderGames();
    }
}
function updateProgress(data) {
    const pct = Math.max(0, Math.min(100, Number(data.percent) || 0));
    $("loader-stage").textContent = data.stage || "Working";
    $("loader-percent").textContent = `${pct.toFixed(1)}%`;
    $("loader-fill").style.width = `${pct}%`;
    $("loader-detail").textContent = data.detail || "";
    $("loader-files").textContent = `${data.filesDone || 0} / ${data.filesTotal || 0}`;
}
async function launchGame(number) {
    if (busy) return;
    busy = true;
    showView("games");
    $("loader").classList.add("active");
    updateProgress({stage:"Preparing game", detail:`Starting game #${number}...`, percent:1});
    recent = [number, ...recent.filter(item => item !== number)].slice(0,12);
    store("codex-recent", recent);
    renderHome();
    renderGames();

    progressTimer = setInterval(async () => {
        try {
            const response = await fetch("/api/progress");
            updateProgress(await response.json());
        } catch (_) {}
    }, 250);

    try {
        const response = await fetch("/api/launch", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({num:number, force_redownload:false})
        });
        const data = await response.json();
        games = data.games || games;
        ports = data.active_ports || ports;
        if (data.progress) updateProgress(data.progress);
        renderGames();
        renderFavorites();
        renderHome();
        renderStats();

        if (data.ok && data.url) {
            try { await navigator.clipboard.writeText(data.url); } catch (_) {}
            toast(`Game is live at ${data.url} — link copied when clipboard access is available.`);
        } else {
            toast(data.status || "Game launch failed.");
        }
    } catch (error) {
        toast(`Launch failed: ${error.message}`);
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
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({port})
        });
        const data = await response.json();
        games = data.games || games;
        ports = data.active_ports || [];
        renderGames();
        renderFavorites();
        renderHome();
        renderStats();
        toast(data.status || "Port stopped.");
    } catch (error) {
        toast(`Could not stop port: ${error.message}`);
    }
}

/* Pointer-reactive particle field: nearby particles are repelled by the cursor;
   clicks send a visible red shockwave through the field. */
const canvas = $("fx-canvas");
const ctx = canvas.getContext("2d");
const pointer = {x:-1000, y:-1000, active:false};
let particles = [];
let ripples = [];
let intensity = 65;
let particlesEnabled = true;
let reducedMotion = false;
let width = 0, height = 0, dpr = 1;

function resizeCanvas() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedParticles();
}
function seedParticles() {
    const count = Math.round((width * height / 14500) * (intensity / 65));
    particles = Array.from({length:Math.max(0, Math.min(115, count))}, () => ({
        x:Math.random()*width, y:Math.random()*height,
        vx:(Math.random()-.5)*.24, vy:(Math.random()-.5)*.24,
        radius:Math.random()*1.5+.45, alpha:Math.random()*.42+.12
    }));
}
function pointerMove(event) {
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    pointer.active = true;
}
window.addEventListener("pointermove", pointerMove, {passive:true});
window.addEventListener("pointerleave", () => { pointer.active = false; });
window.addEventListener("pointerdown", event => {
    if (!particlesEnabled || reducedMotion) return;
    ripples.push({x:event.clientX,y:event.clientY,r:4,alpha:.65});
});
function drawFrame() {
    ctx.clearRect(0,0,width,height);
    if (particlesEnabled) {
        for (const p of particles) {
            if (!reducedMotion) {
                p.x += p.vx; p.y += p.vy;
                if (pointer.active) {
                    const dx=p.x-pointer.x, dy=p.y-pointer.y, dist=Math.hypot(dx,dy);
                    if (dist<135 && dist>0) {
                        const force=(135-dist)/135*.055;
                        p.vx += dx/dist*force;
                        p.vy += dy/dist*force;
                    }
                }
                p.vx *= .994; p.vy *= .994;
                if (p.x<0 || p.x>width) p.vx *= -1;
                if (p.y<0 || p.y>height) p.vy *= -1;
                p.x=Math.max(0,Math.min(width,p.x));
                p.y=Math.max(0,Math.min(height,p.y));
            }

            ctx.beginPath();
            ctx.fillStyle=`rgba(255,55,72,${p.alpha})`;
            ctx.arc(p.x,p.y,p.radius,0,Math.PI*2);
            ctx.fill();

            if (pointer.active) {
                const dx=p.x-pointer.x, dy=p.y-pointer.y, dist=Math.hypot(dx,dy);
                if (dist<115) {
                    ctx.beginPath();
                    ctx.strokeStyle=`rgba(255,55,72,${(1-dist/115)*.15})`;
                    ctx.lineWidth=.7;
                    ctx.moveTo(p.x,p.y);
                    ctx.lineTo(pointer.x,pointer.y);
                    ctx.stroke();
                }
            }
        }
        if (!reducedMotion) {
            ripples = ripples.filter(ripple => ripple.alpha>.015);
            for (const ripple of ripples) {
                ctx.beginPath();
                ctx.strokeStyle=`rgba(255,55,72,${ripple.alpha})`;
                ctx.lineWidth=1.2;
                ctx.arc(ripple.x,ripple.y,ripple.r,0,Math.PI*2);
                ctx.stroke();
                ripple.r += 2.4;
                ripple.alpha *= .965;
            }
        }
    }
    requestAnimationFrame(drawFrame);
}
$("setting-particles").addEventListener("change", event => {
    particlesEnabled = event.target.checked;
});
$("setting-motion").addEventListener("change", event => {
    reducedMotion = event.target.checked;
});
$("setting-intensity").addEventListener("input", event => {
    intensity = Number(event.target.value);
    seedParticles();
});
window.addEventListener("resize", resizeCanvas);
resizeCanvas();
drawFrame();
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
                self.send_json({
                    "games": state.serialize_games(),
                    "active_ports": state.active_ports(),
                    "status": state.status,
                    "quote": dict(state.quote),
                })
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
            length = min(int(self.headers.get("Content-Length", "0")), 16_384)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
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
# Application startup
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
        name="codex-launcher-ui",
    ).start()

    window = webview.create_window(
        title="CODEX-RETURNS // ARCADE",
        url=f"http://127.0.0.1:{ui_port}/",
        width=1366,
        height=820,
        min_size=(860, 600),
        maximized=True,
        background_color="#090707",
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
