import ctypes
import json
import os
import random
import re
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

# ==============================================================================
# 1. PUT YOUR GITHUB REPO LINK HERE
# ==============================================================================
GITHUB_REPO_URL = "https://github.com/Returns-CodeX/CodeX-files"
BRANCH = "main"          # Automatically tries "master" if "main" returns 404
GAMES_FOLDER = "files"   # Searches for folders inside "<repo>/files/<game_name>/"
BASE_PORT = 8000         # Game #12 -> http://127.0.0.1:8012/index.html
MAX_WORKERS = 24         # Parallel downloads for repos with 1,000+ games

# ==============================================================================
# 2. CUSTOM MINECRAFT-STYLE QUOTES (ADD MORE TUPLES ANYTIME)
# ==============================================================================
QUOTES = [
    (
        '"It is fine to get hurt\n'
        ' wounds always heal\n'
        ' anyways, chicks dig scars."',
        "ALI"
    ),
    (
        '"I don\'t hold grudges,\n'
        ' I just remember\n'
        ' who was there."',
        "ALI"
    ),
    (
        '"I don\'t need\n'
        ' a comeback,\n'
        ' I never left and never will."',
        "ALI"
    ),
    (
        '"Scars are just,\n'
        ' proof I showed up\n'
        ' and survived."',
        "ALI"
    ),
]

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
GAMES_CACHE_DIR = CACHE_DIR / "games"
INDEX_CACHE_PATH = CACHE_DIR / "games_index.json"
LAST_QUOTE_PATH = CACHE_DIR / "last_quote.json"

BROWSER_INTRO_SNIPPET = """
<!-- CODEX-RETURNS BROWSER BOOT OVERLAY -->
<style id="codex-boot-style">
#codex-browser-boot {
    position: fixed;
    inset: 0;
    z-index: 2147483647;
    background: radial-gradient(circle at center, #1c0206 0%, #000000 78%);
    color: #f5f5f5;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 14px;
    transition: opacity 0.55s ease, transform 0.55s ease;
}
#codex-browser-boot.codex-fade-out {
    opacity: 0;
    transform: scale(1.03);
    pointer-events: none;
}
#codex-browser-boot .cx-sub {
    font-size: 12px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #d4af37;
}
#codex-browser-boot .cx-logo {
    font-size: clamp(30px, 6vw, 62px);
    font-weight: 900;
    letter-spacing: 4px;
    color: #ff0022;
    text-shadow: 0 0 20px rgba(255, 0, 34, 0.9), 0 0 42px rgba(255, 0, 34, 0.35);
}
#codex-browser-boot .cx-bar {
    width: min(360px, 75vw);
    height: 6px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    overflow: hidden;
}
#codex-browser-boot .cx-bar-fill {
    width: 100%;
    height: 100%;
    transform-origin: left center;
    background: linear-gradient(90deg, #8a0012, #ff0022, #ff3352);
    animation: cxLoad 1.25s cubic-bezier(0.22, 1, 0.36, 1) forwards;
}
#codex-browser-boot .cx-tag {
    font-size: 13px;
    letter-spacing: 2px;
    color: #ff3352;
}
@keyframes cxLoad {
    0% { transform: scaleX(0.05); }
    100% { transform: scaleX(1); }
}
</style>
<div id="codex-browser-boot" aria-hidden="true">
    <div class="cx-sub">Ali's Productions Presents</div>
    <div class="cx-logo">CODEX-RETURNS</div>
    <div class="cx-bar"><div class="cx-bar-fill"></div></div>
    <div class="cx-tag">INITIALIZING GAME STREAM...</div>
</div>
<script id="codex-boot-script">
(function() {
    function dismissBoot() {
        const el = document.getElementById("codex-browser-boot");
        const st = document.getElementById("codex-boot-style");
        const sc = document.getElementById("codex-boot-script");
        if (!el) return;
        el.classList.add("codex-fade-out");
        setTimeout(() => {
            el.remove();
            if (st) st.remove();
            if (sc) sc.remove();
        }, 600);
    }
    if (document.readyState === "complete") {
        setTimeout(dismissBoot, 950);
    } else {
        window.addEventListener("load", () => setTimeout(dismissBoot, 950), { once: true });
        setTimeout(dismissBoot, 2200);
    }
})();
</script>
"""


def hide_console_window():
    """Hide the plain Python terminal window on Windows when running as a desktop app."""
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def maximize_app_window(win):
    """Maximize the desktop window (windowed fullscreen) while keeping title bar controls."""
    try:
        win.maximize()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Ali's Productions presents CodeX-Returns")
            if hwnd:
                user32.ShowWindow(hwnd, 3)
        except Exception:
            pass


def pick_next_quote() -> dict:
    if not QUOTES:
        return {"lines": ['"Stay sharp and keep building."'], "author": "CODEX"}

    last_index = None
    if LAST_QUOTE_PATH.exists():
        try:
            data = json.loads(LAST_QUOTE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("index"), int):
                last_index = data["index"]
        except Exception:
            pass

    candidates = list(range(len(QUOTES)))
    if len(candidates) > 1 and last_index in candidates:
        candidates.remove(last_index)

    chosen_idx = random.choice(candidates)
    try:
        LAST_QUOTE_PATH.write_text(json.dumps({"index": chosen_idx}), encoding="utf-8")
    except Exception:
        pass

    quote_text, author = QUOTES[chosen_idx]
    lines = quote_text.splitlines() or [quote_text]
    return {"lines": lines, "author": author}


def parse_github_repo(repo_url: str) -> tuple[str, str]:
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
        "Set GITHUB_REPO_URL at the top of codex_returns.py to your GitHub repository URL."
    )


def build_session(token: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "CodeX-Returns-Launcher/9.0",
            "Accept": "application/vnd.github+json",
        }
    )
    if token.strip():
        session.headers["Authorization"] = f"Bearer {token.strip()}"
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def find_free_port(preferred: int | None = None) -> tuple[int, bool]:
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred, True
            except OSError:
                pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1]), False


def format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "0.0 KB/s"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.2f} MB/s"
    return f"{bytes_per_sec / 1024:.1f} KB/s"


def format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def copy_to_clipboard(text: str) -> bool:
    if not text:
        return False
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["clip"], input=text.encode("utf-16le"), capture_output=True, timeout=3
            )
            if proc.returncode == 0:
                return True
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    raw = (text + "\x00").encode("utf-16-le")
                    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
                    if h_mem:
                        ptr = kernel32.GlobalLock(h_mem)
                        if ptr:
                            ctypes.memmove(ptr, raw, len(raw))
                            kernel32.GlobalUnlock(h_mem)
                            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                            return True
                finally:
                    user32.CloseClipboard()
            return False
        elif sys.platform == "darwin":
            proc = subprocess.run(
                ["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=3
            )
            return proc.returncode == 0
        else:
            for tool in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    proc = subprocess.run(
                        tool, input=text.encode("utf-8"), capture_output=True, timeout=3
                    )
                    if proc.returncode == 0:
                        return True
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return False


def _parse_lfs_pointer(data: bytes) -> tuple[str, int] | None:
    """Return (oid, size) if bytes represent a Git LFS v1 pointer file, else None."""
    if not data or len(data) > 2048 or b"https://git-lfs.github.com/spec/v1" not in data:
        return None
    text = data.decode("utf-8", errors="ignore")
    oid_match = re.search(r"^oid sha256:([0-9a-f]{64})\s*$", text, re.MULTILINE)
    size_match = re.search(r"^size (\d+)\s*$", text, re.MULTILINE)
    if not oid_match or not size_match:
        return None
    return oid_match.group(1), int(size_match.group(1))


def _resolve_lfs_download_url(
    session: requests.Session, owner: str, repo: str, oid: str, size: int
) -> str:
    """Resolve real binary download URL from GitHub's Git LFS Batch API."""
    batch_url = f"https://github.com/{quote(owner)}/{quote(repo)}.git/info/lfs/objects/batch"
    payload = {
        "operation": "download",
        "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
    }
    headers = {
        "Accept": "application/vnd.git-lfs+json",
        "Content-Type": "application/vnd.git-lfs+json",
    }
    if "Authorization" in session.headers:
        headers["Authorization"] = session.headers["Authorization"]
    resp = session.post(batch_url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    objects = body.get("objects") or []
    if not objects:
        raise RuntimeError(f"Git LFS returned no object entry for {oid[:12]}...")
    obj = objects[0]
    if "error" in obj:
        err_msg = obj["error"].get("message", "Unknown Git LFS error")
        raise RuntimeError(f"Git LFS object error: {err_msg}")
    href = ((obj.get("actions") or {}).get("download") or {}).get("href")
    if not href:
        raise RuntimeError(f"Git LFS download URL missing for {oid[:12]}...")
    return href


class GameBootHTTPHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        clean_path = parsed.path
        if clean_path in ("", "/"):
            clean_path = "/index.html"

        fs_path = Path(self.translate_path(clean_path))
        if fs_path.is_file() and fs_path.suffix.lower() in (".html", ".htm"):
            try:
                raw = fs_path.read_text(encoding="utf-8", errors="replace")
                if "codex-browser-boot" not in raw:
                    if "<body" in raw.lower():
                        raw = re.sub(
                            r"(<body[^>]*>)",
                            r"\1" + BROWSER_INTRO_SNIPPET,
                            raw,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                    else:
                        raw = BROWSER_INTRO_SNIPPET + raw
                encoded = raw.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            except Exception:
                pass

        super().do_GET()


class QuietUIHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Ali's Productions presents CodeX-Returns</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Audiowide&family=Rajdhani:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
:root {
    --primary: #ff0022;
    --primary-dark: #a80016;
    --primary-light: #ff2e4c;
    --gold: #d4af37;
    --gold-subtle: rgba(212, 175, 55, 0.28);
    --green: #22ff66;
    --green-dark: #00b844;
    --green-glow: 0 0 14px rgba(34, 255, 102, 0.72);
    --dark: #070708;
    --darker: #000000;
    --panel: rgba(10, 10, 11, 0.92);
    --card-top: #13080a;
    --card-bottom: #050506;
    --text: #f4f4f6;
    --text-muted: #969298;
    --glow-red: 0 0 18px rgba(255, 0, 34, 0.45);
    --glow-red-strong: 0 0 28px rgba(255, 0, 34, 0.68);
}

*, *::before, *::after {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    background-color: var(--darker);
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
    line-height: 1.55;
}

h1, h2, h3, h4, h5, h6 {
    font-family: 'Audiowide', cursive;
    font-weight: 400;
    letter-spacing: 1.2px;
}

::-webkit-scrollbar {
    width: 7px;
}
::-webkit-scrollbar-track {
    background: var(--darker);
}
::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, var(--primary-dark), var(--primary));
    border-radius: 4px;
}

@keyframes pulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.025); }
}

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}

@keyframes rotateGradient {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

@keyframes shimmerBar {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}

@keyframes bootSweep {
    0% { left: -45%; }
    100% { left: 115%; }
}

.bg-radial {
    position: fixed;
    inset: 0;
    background:
        radial-gradient(circle at 22% 14%, rgba(255, 0, 34, 0.18), transparent 46%),
        radial-gradient(circle at 80% 80%, rgba(255, 0, 34, 0.1), transparent 50%),
        var(--darker);
    pointer-events: none;
    z-index: 0;
}

.bg-grid {
    position: fixed;
    inset: 0;
    background-image:
        linear-gradient(rgba(255, 0, 34, 0.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 0, 34, 0.05) 1px, transparent 1px);
    background-size: 32px 32px;
    pointer-events: none;
    z-index: 1;
}

/* Console Startup Splash (No quotes at startup) */
#splash {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    background: radial-gradient(circle at center, rgba(255, 0, 34, 0.2), #000000 80%);
    transition: opacity 0.45s ease, transform 0.45s ease;
}

#splash.fade-out {
    opacity: 0;
    transform: scale(1.025);
    pointer-events: none;
}

.console-splash-shell {
    width: 100%;
    max-width: 760px;
    background: linear-gradient(155deg, rgba(16, 8, 10, 0.97), rgba(3, 3, 4, 0.99));
    border: 1px solid rgba(255, 0, 34, 0.48);
    border-radius: 6px;
    padding: 2.6rem 2.4rem;
    position: relative;
    overflow: hidden;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.95), var(--glow-red-strong);
    animation: fadeIn 0.4s ease-out;
}

.console-splash-shell::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
}

.console-splash-shell::after {
    content: '';
    position: absolute;
    top: 0;
    bottom: 0;
    width: 32%;
    background: linear-gradient(90deg, transparent, rgba(255, 0, 34, 0.09), transparent);
    animation: bootSweep 2.1s linear infinite;
    pointer-events: none;
}

.console-top-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-family: 'Audiowide', cursive;
    font-size: 0.75rem;
    letter-spacing: 2px;
    color: var(--primary-light);
    margin-bottom: 1.4rem;
}

.console-led-group {
    display: flex;
    align-items: center;
    gap: 0.45rem;
}

.console-led {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--primary);
    box-shadow: 0 0 8px var(--primary);
}

.console-led.gold {
    background: var(--gold);
    box-shadow: 0 0 8px var(--gold);
}

.console-led.green {
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
}

.splash-presenter {
    text-align: center;
    font-size: 0.98rem;
    font-weight: 700;
    letter-spacing: 3.5px;
    text-transform: uppercase;
    color: var(--gold);
    margin-bottom: 0.4rem;
}

.splash-console-title {
    text-align: center;
    font-size: clamp(2.2rem, 5.2vw, 3.6rem);
    color: var(--primary);
    text-shadow: 0 0 14px rgba(255, 0, 34, 0.85);
    animation: pulse 2.4s infinite ease-in-out;
    margin-bottom: 0.35rem;
}

.splash-console-sub {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.98rem;
    letter-spacing: 2px;
    margin-bottom: 1.6rem;
}

.boot-diag-box {
    background: rgba(0, 0, 0, 0.8);
    border: 1px solid rgba(255, 0, 34, 0.24);
    border-radius: 5px;
    padding: 1rem 1.2rem;
    margin-bottom: 1.4rem;
    font-size: 0.98rem;
}

.boot-diag-line {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.22rem 0;
    color: var(--text-muted);
}

.boot-diag-line strong {
    color: var(--text);
    font-family: 'Audiowide', cursive;
    font-size: 0.76rem;
}

.boot-diag-line span.ok {
    color: var(--primary-light);
    font-weight: 700;
    letter-spacing: 1px;
}

.boot-gauge-wrap {
    margin-bottom: 1.2rem;
}

.boot-gauge-meta {
    display: flex;
    justify-content: space-between;
    font-family: 'Audiowide', cursive;
    font-size: 0.82rem;
    margin-bottom: 0.45rem;
}

.boot-gauge-label {
    color: var(--text);
}

.boot-gauge-pct {
    color: var(--primary-light);
}

.boot-gauge-track {
    width: 100%;
    height: 10px;
    background: #0b0708;
    border-radius: 999px;
    overflow: hidden;
    border: 1px solid rgba(255, 0, 34, 0.38);
}

.boot-gauge-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--primary-light));
    transition: width 0.16s ease;
    box-shadow: 0 0 12px rgba(255, 0, 34, 0.65);
}

.splash-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}

.splash-status-text {
    color: var(--text-muted);
    font-size: 0.95rem;
    font-weight: 600;
}

.enter-console-btn {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(255, 0, 34, 0.65);
    border-radius: 4px;
    padding: 0.55rem 1.2rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.76rem;
    letter-spacing: 1px;
    cursor: pointer;
    font-weight: 700;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.enter-console-btn:hover {
    transform: translateY(-2px);
    box-shadow: var(--glow-red);
}

/* Header, Active Ports Top Bar & Main Layout */
header {
    background: rgba(0, 0, 0, 0.95);
    backdrop-filter: blur(12px);
    padding: 0.85rem 2rem;
    box-shadow: 0 4px 24px rgba(0, 0, 0, 0.92), var(--glow-red);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    position: sticky;
    top: 0;
    z-index: 40;
    border-bottom: 1px solid rgba(255, 0, 34, 0.3);
}

header .presenter-line {
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 3.2px;
    text-transform: uppercase;
    color: var(--gold);
    margin-bottom: 0.1rem;
}

header .main-title {
    font-size: 2.1rem;
    color: var(--primary);
    text-shadow: 0 0 10px rgba(255, 0, 34, 0.85);
}

main {
    flex: 1;
    padding: 1.4rem 2rem 2.2rem;
    max-width: 1440px;
    margin: 0 auto;
    width: 100%;
    position: relative;
    z-index: 5;
    display: flex;
    flex-direction: column;
    align-items: center;
    animation: fadeIn 0.6s ease-out;
}

.active-ports-bar {
    width: 100%;
    background: linear-gradient(135deg, rgba(16, 7, 9, 0.95), rgba(5, 5, 6, 0.98));
    border: 1px solid rgba(255, 0, 34, 0.36);
    border-radius: 6px;
    padding: 0.72rem 1.2rem;
    margin-bottom: 1.15rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.9rem;
    flex-wrap: wrap;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.8);
}

.active-ports-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.8rem;
    color: var(--primary-light);
    letter-spacing: 1.4px;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.active-ports-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--primary);
    box-shadow: 0 0 8px var(--primary);
}

.active-ports-list {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.6rem;
}

.active-ports-empty {
    color: var(--text-muted);
    font-size: 0.92rem;
}

.port-pill-group {
    display: inline-flex;
    align-items: stretch;
    background: rgba(255, 0, 34, 0.12);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 4px;
    overflow: hidden;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.port-pill-group:hover {
    border-color: var(--primary-light);
    box-shadow: var(--glow-red);
}

.port-pill-copy {
    background: transparent;
    border: none;
    color: var(--text);
    padding: 0.34rem 0.72rem;
    font-family: 'Rajdhani', sans-serif;
    font-size: 0.94rem;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    transition: background 0.2s ease;
}

.port-pill-copy strong {
    font-family: 'Audiowide', cursive;
    font-size: 0.75rem;
    color: var(--gold);
}

.port-pill-copy:hover {
    background: rgba(255, 0, 34, 0.22);
}

.port-pill-stop {
    background: rgba(255, 0, 34, 0.28);
    border: none;
    border-left: 1px solid rgba(255, 0, 34, 0.45);
    color: #ffffff;
    padding: 0.34rem 0.62rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.68rem;
    letter-spacing: 0.6px;
    cursor: pointer;
    transition: background 0.2s ease;
}

.port-pill-stop:hover {
    background: var(--primary);
    color: #ffffff;
}

.quote-banner {
    width: 100%;
    background: linear-gradient(135deg, rgba(16, 7, 9, 0.95), rgba(6, 6, 7, 0.97));
    border: 1px solid rgba(255, 0, 34, 0.34);
    border-radius: 6px;
    padding: 1.15rem 1.5rem;
    margin-bottom: 1.3rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    position: relative;
    overflow: hidden;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.8);
}

.quote-banner::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 3px;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
}

.quote-lines {
    font-family: 'Rajdhani', sans-serif;
    font-size: 1.28rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.45;
}

.quote-line {
    display: block;
}

.quote-footer-bar {
    width: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1rem;
    margin-top: 0.65rem;
    flex-wrap: wrap;
}

.quote-author {
    font-family: 'Audiowide', cursive;
    font-size: 0.86rem;
    color: var(--gold);
    letter-spacing: 2.5px;
}

.cycle-quote-btn {
    background: rgba(255, 0, 34, 0.14);
    color: var(--primary-light);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 4px;
    padding: 0.34rem 0.85rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.7rem;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all 0.2s ease;
}

.cycle-quote-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #ffffff;
    box-shadow: var(--glow-red);
    transform: translateY(-1px);
}

.cycle-quote-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

.content {
    width: 100%;
    background: var(--panel);
    backdrop-filter: blur(12px);
    padding: 2rem;
    border-radius: 6px;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.85), var(--glow-red);
    border: 1px solid rgba(255, 0, 34, 0.28);
    position: relative;
    overflow: hidden;
}

.content::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 3px;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
}

.controls-top {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    align-items: center;
    gap: 0.95rem;
    margin-bottom: 1.2rem;
}

.num-input-group {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    background: rgba(0, 0, 0, 0.88);
    border: 1px solid rgba(255, 0, 34, 0.5);
    border-radius: 5px;
    padding: 0.35rem 0.9rem;
    box-shadow: 0 0 12px rgba(255, 0, 34, 0.16);
}

.num-input-group label {
    font-family: 'Audiowide', cursive;
    color: var(--primary-light);
    font-size: 0.98rem;
}

#game-num-input {
    width: 96px;
    background: transparent;
    border: none;
    color: var(--text);
    font-family: 'Audiowide', cursive;
    font-size: 1.2rem;
    text-align: center;
    padding: 0.3rem;
}

#game-num-input:focus {
    outline: none;
}

.action-btn {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(255, 0, 34, 0.65);
    padding: 0.78rem 1.55rem;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
    font-family: 'Audiowide', cursive;
    letter-spacing: 1px;
    font-size: 0.86rem;
    transition: all 0.25s ease;
    box-shadow: 0 6px 18px rgba(255, 0, 34, 0.36);
}

.action-btn:hover:not(:disabled) {
    background: linear-gradient(135deg, var(--primary), var(--primary-light));
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(255, 0, 34, 0.65);
}

.action-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.secondary-btn {
    background: rgba(255, 0, 34, 0.08);
    color: var(--text);
    border: 1px solid rgba(255, 0, 34, 0.42);
    box-shadow: none;
}

.secondary-btn:hover:not(:disabled) {
    background: rgba(255, 0, 34, 0.24);
    color: #ffffff;
    border-color: var(--primary);
    box-shadow: var(--glow-red);
}

.controls-sub {
    display: flex;
    justify-content: center;
    margin-bottom: 1.3rem;
}

.controls-sub.hidden {
    display: none;
}

.search-bar {
    display: flex;
    align-items: center;
    background: rgba(0, 0, 0, 0.88);
    border: 1px solid rgba(255, 0, 34, 0.36);
    border-radius: 5px;
    padding: 0.35rem 0.9rem;
    width: 100%;
    max-width: 540px;
    transition: all 0.25s ease;
}

.search-bar:focus-within {
    border-color: var(--primary);
    box-shadow: var(--glow-red);
}

.search-bar input {
    background: transparent;
    border: none;
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    font-size: 1.08rem;
    width: 100%;
    padding: 0.35rem 0;
}

.search-bar input:focus {
    outline: none;
}

.clear-search-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-family: 'Audiowide', cursive;
    font-size: 0.75rem;
    padding: 0.25rem 0.5rem;
    display: none;
}

.clear-search-btn:hover {
    color: var(--primary);
}

/* Focused Game Loading Page */
.launch-panel {
    display: none;
    width: 100%;
    max-width: 880px;
    margin: 0 auto 1.4rem;
    padding: 1.6rem 1.75rem;
    background: linear-gradient(155deg, rgba(16, 8, 10, 0.98), rgba(3, 3, 4, 0.99));
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 6px;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.9), var(--glow-red);
    animation: fadeIn 0.28s ease-out;
}

.launch-panel.active {
    display: block;
}

.launch-top-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
    padding-bottom: 0.85rem;
    margin-bottom: 0.95rem;
    border-bottom: 1px solid rgba(255, 0, 34, 0.22);
}

.launch-game-heading {
    font-family: 'Audiowide', cursive;
    font-size: 1.15rem;
    color: var(--text);
}

.launch-game-heading span {
    color: var(--gold);
}

.launch-page-controls {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    flex-wrap: wrap;
}

.force-toggle-btn {
    background: rgba(255, 255, 255, 0.03);
    color: var(--text-muted);
    border: 1px solid rgba(255, 0, 34, 0.35);
    border-radius: 4px;
    padding: 0.46rem 0.85rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.7rem;
    letter-spacing: 0.8px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
}

.force-toggle-btn .toggle-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #4a4a52;
    transition: background 0.2s ease, box-shadow 0.2s ease;
}

.force-toggle-btn:hover:not(:disabled) {
    border-color: var(--primary-light);
    color: var(--text);
}

.force-toggle-btn.active {
    background: rgba(255, 0, 34, 0.22);
    border-color: var(--primary);
    color: #ffffff;
    box-shadow: var(--glow-red);
}

.force-toggle-btn.active .toggle-dot {
    background: var(--primary);
    box-shadow: 0 0 8px var(--primary);
}

.force-toggle-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.back-grid-btn {
    background: rgba(255, 255, 255, 0.04);
    color: var(--text);
    border: 1px solid rgba(255, 0, 34, 0.35);
    border-radius: 4px;
    padding: 0.46rem 0.85rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.7rem;
    letter-spacing: 0.8px;
    cursor: pointer;
    transition: all 0.2s ease;
}

.back-grid-btn:hover:not(:disabled) {
    background: rgba(255, 0, 34, 0.2);
    border-color: var(--primary);
}

.back-grid-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.launch-header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.4rem;
    flex-wrap: wrap;
}

.launch-stage {
    font-family: 'Audiowide', cursive;
    font-size: 0.96rem;
    color: var(--primary-light);
}

.launch-percent-green {
    font-family: 'Audiowide', cursive;
    font-size: 1.25rem;
    color: var(--green);
    text-shadow: var(--green-glow);
}

.launch-detail {
    font-size: 1.02rem;
    color: var(--text);
    margin-bottom: 0.85rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.progress-track {
    width: 100%;
    height: 16px;
    background: #070a08;
    border: 1px solid rgba(34, 255, 102, 0.45);
    border-radius: 4px;
    overflow: hidden;
}

.progress-fill-green {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--green-dark), var(--green), #8affb2, var(--green));
    background-size: 200% 100%;
    box-shadow: var(--green-glow);
    transition: width 0.14s ease-out;
    animation: shimmerBar 1.35s linear infinite;
}

.launch-footer-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.6rem;
    gap: 1rem;
    flex-wrap: wrap;
}

.launch-speed {
    font-family: 'Audiowide', cursive;
    font-size: 0.9rem;
    color: var(--green);
}

.launch-submeta {
    color: var(--text-muted);
    font-size: 0.94rem;
}

.ready-action-area {
    display: none;
    margin-top: 1.15rem;
    padding-top: 1.05rem;
    border-top: 1px solid rgba(255, 0, 34, 0.25);
}

.ready-action-area.visible {
    display: block;
    animation: fadeIn 0.25s ease-out;
}

.ready-btn-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
}

.complete-action-btn {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(212, 175, 55, 0.55);
    border-radius: 4px;
    padding: 0.72rem 1.35rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.8rem;
    letter-spacing: 1px;
    cursor: pointer;
    box-shadow: 0 6px 20px rgba(255, 0, 34, 0.45);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}

.complete-action-btn:hover {
    background: linear-gradient(135deg, var(--primary), var(--primary-light));
    transform: translateY(-2px);
    box-shadow: var(--glow-red-strong);
}

.port-guide-box {
    display: none;
    margin-top: 0.95rem;
    padding: 1rem 1.15rem;
    background: rgba(0, 0, 0, 0.92);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 5px;
}

.port-guide-box.active {
    display: block;
}

.port-url-code {
    display: inline-block;
    font-family: 'Audiowide', cursive;
    font-size: 0.9rem;
    color: var(--gold);
    background: rgba(255, 0, 34, 0.08);
    border: 1px solid rgba(255, 0, 34, 0.35);
    padding: 0.45rem 0.75rem;
    border-radius: 4px;
    word-break: break-all;
    margin-bottom: 0.65rem;
}

.port-steps-list {
    list-style: none;
    font-size: 0.98rem;
    color: var(--text);
    display: grid;
    gap: 0.25rem;
}

.port-steps-list li span.step-badge {
    color: var(--primary-light);
    font-weight: 700;
    margin-right: 0.35rem;
}

.port-steps-list li kbd {
    background: rgba(255, 0, 34, 0.14);
    border: 1px solid rgba(255, 0, 34, 0.4);
    border-radius: 3px;
    padding: 0.05rem 0.38rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.74rem;
    color: var(--text);
}

#status-msg {
    text-align: center;
    color: var(--primary-light);
    font-weight: 600;
    font-size: 1.02rem;
    margin-bottom: 1.3rem;
    min-height: 1.5rem;
}

/* Games Grid & Cards */
.games-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(275px, 1fr));
    gap: 1.5rem;
    width: 100%;
}

.games-grid.hidden {
    display: none;
}

.game-card {
    background: linear-gradient(155deg, var(--card-top), var(--card-bottom));
    border-radius: 6px;
    padding: 1.45rem;
    text-align: center;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.8);
    transition: all 0.28s ease;
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255, 0, 34, 0.22);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    min-height: 215px;
    animation: fadeIn 0.42s ease-out both;
}

.game-card::before {
    content: '';
    position: absolute;
    top: -55%;
    left: -55%;
    width: 210%;
    height: 210%;
    background: linear-gradient(
        to bottom right,
        transparent,
        rgba(255, 0, 34, 0.11),
        rgba(212, 175, 55, 0.04),
        transparent
    );
    transform: rotate(30deg);
    z-index: 1;
    opacity: 0;
    pointer-events: none;
}

.game-card:hover {
    transform: translateY(-7px);
    box-shadow: 0 16px 32px rgba(0, 0, 0, 0.9), var(--glow-red);
    border-color: var(--primary);
}

.game-card:hover::before {
    opacity: 1;
    animation: rotateGradient 9s linear infinite;
}

.game-num {
    font-family: 'Audiowide', cursive;
    font-size: 0.84rem;
    color: var(--primary-light);
    z-index: 2;
    margin-bottom: 0.3rem;
}

.game-card h3 {
    color: var(--text);
    margin-bottom: 0.65rem;
    font-size: 1.28rem;
    position: relative;
    display: inline-block;
    z-index: 2;
    word-break: break-word;
}

.game-card h3::after {
    content: '';
    position: absolute;
    bottom: -5px;
    left: 50%;
    transform: translateX(-50%);
    width: 42px;
    height: 2px;
    background: linear-gradient(90deg, var(--primary), var(--gold));
    border-radius: 2px;
    transition: width 0.25s ease;
}

.game-card:hover h3::after {
    width: 82px;
}

.game-meta-pills {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 0.45rem;
    z-index: 2;
    margin: 0.55rem 0 0.5rem;
}

.meta-pill {
    font-size: 0.82rem;
    font-weight: 700;
    padding: 0.16rem 0.55rem;
    border-radius: 999px;
    background: rgba(255, 0, 34, 0.1);
    border: 1px solid rgba(255, 0, 34, 0.32);
    color: var(--text);
}

.meta-pill.gold-accent {
    color: var(--gold);
    border-color: var(--gold-subtle);
    background: rgba(212, 175, 55, 0.08);
}

.game-port {
    font-size: 0.9rem;
    color: var(--text-muted);
    z-index: 2;
    margin-bottom: 0.85rem;
}

.cached-tag {
    color: var(--green);
    font-weight: 700;
}

.game-card button {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(255, 0, 34, 0.55);
    padding: 0.68rem 1.2rem;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
    font-family: 'Audiowide', cursive;
    letter-spacing: 1px;
    font-size: 0.8rem;
    transition: all 0.25s ease;
    z-index: 2;
    width: 100%;
}

.game-card button:hover:not(:disabled) {
    background: linear-gradient(135deg, var(--primary), var(--primary-light));
    box-shadow: var(--glow-red);
}

.game-card button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.no-results {
    grid-column: 1 / -1;
    text-align: center;
    padding: 2.5rem;
    color: var(--text-muted);
    font-size: 1.15rem;
}

footer {
    background: rgba(0, 0, 0, 0.97);
    backdrop-filter: blur(10px);
    text-align: center;
    padding: 1.15rem;
    margin-top: auto;
    border-top: 1px solid rgba(255, 0, 34, 0.28);
    position: relative;
    z-index: 5;
}

footer::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--primary), var(--gold), transparent);
}

footer p {
    color: var(--text-muted);
    font-size: 0.9rem;
}
</style>
</head>
<body>
<div class="bg-radial"></div>
<div class="bg-grid"></div>

<!-- Console Startup Splash Screen (Quotes intentionally omitted at startup) -->
<div id="splash">
    <div class="console-splash-shell">
        <div class="console-top-bar">
            <span>CODEX-SYS // KERNEL v9.0</span>
            <div class="console-led-group">
                <span class="console-led"></span>
                <span class="console-led gold"></span>
                <span class="console-led green"></span>
            </div>
        </div>

        <div class="splash-presenter">Ali's Productions Presents</div>
        <h1 class="splash-console-title">CODEX-RETURNS</h1>
        <p class="splash-console-sub">LOCALHOST ARCADE CONSOLE &bull; MULTI-PORT ENGINE</p>

        <div class="boot-diag-box">
            <div class="boot-diag-line">
                <strong>[01] GRAPHICS &amp; SHADER BUS</strong>
                <span class="ok">ONLINE</span>
            </div>
            <div class="boot-diag-line">
                <strong>[02] CACHE VAULT (.cache/games)</strong>
                <span class="ok">MOUNTED</span>
            </div>
            <div class="boot-diag-line">
                <strong>[03] LOCALHOST PORT ROUTER</strong>
                <span class="ok">ARMED</span>
            </div>
        </div>

        <div class="boot-gauge-wrap">
            <div class="boot-gauge-meta">
                <span id="boot-gauge-label" class="boot-gauge-label">Booting console firmware...</span>
                <span id="boot-gauge-pct" class="boot-gauge-pct">0%</span>
            </div>
            <div class="boot-gauge-track">
                <div id="boot-gauge-fill" class="boot-gauge-fill"></div>
            </div>
        </div>

        <div class="splash-actions">
            <div id="splash-status-text" class="splash-status-text">Powering on CodeX-Returns console...</div>
            <button type="button" class="enter-console-btn" onclick="dismissSplash(true)">ENTER CONSOLE</button>
        </div>
    </div>
</div>

<header>
    <div class="presenter-line">Ali's Productions Presents</div>
    <h1 class="main-title">CODEX-RETURNS</h1>
</header>

<main>
    <section id="active-ports-bar" class="active-ports-bar">
        <div class="active-ports-title">
            <span class="active-ports-dot"></span>
            <span>ACTIVE PORTS</span>
        </div>
        <div id="active-ports-list" class="active-ports-list">
            <span class="active-ports-empty">No game ports running yet. Launch a game to bind a port.</span>
        </div>
    </section>

    <section id="quote-banner" class="quote-banner">
        <div id="quote-lines" class="quote-lines"></div>
        <div class="quote-footer-bar">
            <div id="quote-author" class="quote-author"></div>
            <button id="cycle-quote-btn" class="cycle-quote-btn" type="button" onclick="cycleQuote()">
                NEXT QUOTE
            </button>
        </div>
    </section>

    <section class="content">
        <div class="controls-top">
            <div class="num-input-group">
                <label for="game-num-input">GAME #</label>
                <input id="game-num-input" type="number" min="1" />
            </div>
            <button id="launch-btn" class="action-btn" onclick="launchByNumber()">FETCH &amp; RUN</button>
            <button id="sync-btn" class="action-btn secondary-btn" onclick="syncRepo()">SYNC REPO</button>
        </div>

        <div id="search-row" class="controls-sub">
            <div class="search-bar">
                <input id="search-input" type="text" placeholder="Search game name or number..." oninput="onSearchInput()" />
                <button id="clear-search-btn" class="clear-search-btn" type="button" onclick="clearSearch()">CLEAR</button>
            </div>
        </div>

        <div id="launch-panel" class="launch-panel">
            <div class="launch-top-meta">
                <div id="launch-game-heading" class="launch-game-heading">GAME LOADER</div>
                <div class="launch-page-controls">
                    <button id="force-toggle-btn" class="force-toggle-btn" type="button" onclick="toggleForceRedownload()">
                        <span class="toggle-dot"></span>
                        <span>FORCE REDOWNLOAD: OFF</span>
                    </button>
                    <button id="back-grid-btn" class="back-grid-btn" type="button" onclick="backToGamesGrid()">
                        BROWSE GAMES
                    </button>
                </div>
            </div>

            <div class="launch-header-row">
                <div id="launch-stage" class="launch-stage">Fetching file</div>
                <div id="launch-percent" class="launch-percent-green">0.0%</div>
            </div>
            <div id="launch-detail" class="launch-detail">Preparing game files...</div>
            <div class="progress-track">
                <div id="progress-fill" class="progress-fill-green"></div>
            </div>
            <div class="launch-footer-row">
                <div id="launch-speed" class="launch-speed">Speed: 0.0 KB/s</div>
                <div id="launch-submeta" class="launch-submeta">0 / 0 files</div>
            </div>

            <div id="ready-action-area" class="ready-action-area">
                <div class="ready-btn-row">
                    <button id="complete-action-btn" class="complete-action-btn" type="button" onclick="onReadyActionPressed()">
                        COPY GAME LINK &amp; SHOW STEPS
                    </button>
                    <button id="reload-game-btn" class="back-grid-btn" type="button" onclick="reloadCurrentGame()">
                        RUN AGAIN
                    </button>
                </div>

                <div id="port-guide-box" class="port-guide-box active">
                    <code id="port-url-display" class="port-url-code"></code>
                    <ul class="port-steps-list">
                        <li><span class="step-badge">1.</span> Open your browser (link is auto-copied to your clipboard).</li>
                        <li><span class="step-badge">2.</span> Click the search / address bar and press <kbd>Ctrl+V</kbd>.</li>
                        <li><span class="step-badge">3.</span> Press <kbd>Enter</kbd> and wait for it to load.</li>
                    </ul>
                </div>
            </div>
        </div>

        <div id="status-msg">Initializing...</div>

        <div id="games-grid" class="games-grid"></div>
    </section>
</main>

<footer>
    <p>Ali's Productions &bull; CodeX-Returns &bull; Cache folder: <code>.cache/</code></p>
</footer>

<script>
let gamesData = [];
let activePortsData = [];
let busy = false;
let forceRedownload = false;
let selectedGameNum = null;
let latestLaunchUrl = "";
let splashDismissed = false;

(function runConsoleBoot() {
    const steps = [
        { pct: 24, label: "Checking local cache & port table...", status: "SYS.INIT // Verifying local directories" },
        { pct: 58, label: "Warming up multi-port HTTP engine...", status: "BUS.SCAN // Binding localhost sockets" },
        { pct: 86, label: "Synchronizing arcade game index...", status: "INDEX.LOAD // Reading repository tree" },
        { pct: 100, label: "Console ready.", status: "SYS.READY // Welcome to CodeX-Returns" },
    ];

    let i = 0;
    const fillEl = document.getElementById("boot-gauge-fill");
    const pctEl = document.getElementById("boot-gauge-pct");
    const labelEl = document.getElementById("boot-gauge-label");
    const statusEl = document.getElementById("splash-status-text");

    const interval = setInterval(() => {
        if (splashDismissed || i >= steps.length) {
            clearInterval(interval);
            setTimeout(() => dismissSplash(false), 320);
            return;
        }
        const s = steps[i++];
        fillEl.style.width = `${s.pct}%`;
        pctEl.textContent = `${s.pct}%`;
        labelEl.textContent = s.label;
        statusEl.textContent = s.status;
    }, 320);
})();

function dismissSplash(immediate) {
    if (splashDismissed) return;
    splashDismissed = true;
    const splash = document.getElementById("splash");
    if (!splash) return;
    splash.classList.add("fade-out");
    setTimeout(() => {
        splash.style.display = "none";
    }, immediate ? 180 : 480);
}

document.getElementById("game-num-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") launchByNumber();
});

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function setLoaderView(showLoader) {
    const panel = document.getElementById("launch-panel");
    const grid = document.getElementById("games-grid");
    const searchRow = document.getElementById("search-row");

    if (showLoader) {
        panel.classList.add("active");
        grid.classList.add("hidden");
        searchRow.classList.add("hidden");
    } else {
        panel.classList.remove("active");
        grid.classList.remove("hidden");
        searchRow.classList.remove("hidden");
    }
}

function backToGamesGrid() {
    if (busy) return;
    setLoaderView(false);
}

function toggleForceRedownload() {
    forceRedownload = !forceRedownload;
    const btn = document.getElementById("force-toggle-btn");
    btn.classList.toggle("active", forceRedownload);
    btn.innerHTML = `<span class="toggle-dot"></span><span>FORCE REDOWNLOAD: ${forceRedownload ? "ON" : "OFF"}</span>`;
}

function renderActivePorts(ports) {
    activePortsData = Array.isArray(ports) ? ports : [];
    const listEl = document.getElementById("active-ports-list");
    if (!activePortsData.length) {
        listEl.innerHTML = '<span class="active-ports-empty">No game ports running yet. Launch a game to bind a port.</span>';
        return;
    }
    listEl.innerHTML = activePortsData.map(p => `
        <div class="port-pill-group">
            <button type="button" class="port-pill-copy" title="Copy ${escapeHtml(p.url)}" onclick="copySpecificPort('${escapeHtml(p.url)}', this)">
                <strong>PORT ${p.port}</strong>
                <span>#${p.num} ${escapeHtml(p.name)}</span>
            </button>
            <button type="button" class="port-pill-stop" title="Stop Port ${p.port}" onclick="stopPort(${p.port})">
                STOP
            </button>
        </div>
    `).join("");
}

async function stopPort(port) {
    try {
        const res = await fetch("/api/stop_port", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ port: port })
        });
        const data = await res.json();
        if (data.active_ports) {
            renderActivePorts(data.active_ports);
        }
        if (data.games) {
            gamesData = data.games;
            renderGames();
        }
        if (latestLaunchUrl && latestLaunchUrl.includes(`:${port}/`)) {
            latestLaunchUrl = "";
            document.getElementById("ready-action-area").classList.remove("visible");
        }
        if (data.status) {
            document.getElementById("status-msg").textContent = data.status;
        }
    } catch (err) {
        document.getElementById("status-msg").textContent = "Failed to stop port: " + err;
    }
}

async function copySpecificPort(url, btnEl) {
    const ok = await copyTextFallback(url);
    latestLaunchUrl = url;
    document.getElementById("port-url-display").textContent = url;
    document.getElementById("ready-action-area").classList.add("visible");
    if (btnEl) {
        const prev = btnEl.innerHTML;
        btnEl.innerHTML = ok ? `<strong>COPIED</strong> <span>${escapeHtml(url)}</span>` : prev;
        setTimeout(() => { btnEl.innerHTML = prev; }, 1400);
    }
}

function renderQuote(quoteObj) {
    if (!quoteObj) return;
    const linesHtml = (quoteObj.lines || [])
        .map(line => `<span class="quote-line">${escapeHtml(line)}</span>`)
        .join("");
    document.getElementById("quote-lines").innerHTML = linesHtml;
    document.getElementById("quote-author").textContent = `— ${quoteObj.author || "CODEX"}`;
}

async function cycleQuote() {
    const btn = document.getElementById("cycle-quote-btn");
    btn.disabled = true;
    try {
        const res = await fetch("/api/quote");
        const data = await res.json();
        if (data.quote) {
            renderQuote(data.quote);
        }
    } catch (_) {}
    btn.disabled = false;
}

function setBusyState(state, msg) {
    busy = state;
    document.getElementById("launch-btn").disabled = state;
    document.getElementById("sync-btn").disabled = state;
    document.getElementById("force-toggle-btn").disabled = state;
    document.getElementById("back-grid-btn").disabled = state;
    document.querySelectorAll(".game-card button").forEach(btn => {
        btn.disabled = state;
    });
    if (msg !== undefined) {
        document.getElementById("status-msg").textContent = msg;
    }
}

function showLaunchProgress({ stage, detail, percent, speed, filesDone, filesTotal }) {
    const clamped = Math.max(0, Math.min(100, Number(percent) || 0));
    document.getElementById("launch-stage").textContent = stage || "Fetching file";
    document.getElementById("launch-detail").textContent = detail || "";
    document.getElementById("launch-percent").textContent = `${clamped.toFixed(1)}%`;
    document.getElementById("progress-fill").style.width = `${clamped}%`;
    document.getElementById("launch-speed").textContent = `Speed: ${speed || "0.0 KB/s"}`;
    document.getElementById("launch-submeta").textContent =
        filesTotal > 0 ? `${filesDone} / ${filesTotal} files` : "Building files";

    const readyArea = document.getElementById("ready-action-area");
    if (clamped >= 100 && latestLaunchUrl) {
        readyArea.classList.add("visible");
    } else if (clamped < 100) {
        readyArea.classList.remove("visible");
    }
}

async function copyTextFallback(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (_) {}
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try {
        ok = document.execCommand("copy");
    } catch (_) {
        ok = false;
    }
    ta.remove();
    return ok;
}

async function onReadyActionPressed() {
    if (!latestLaunchUrl) return;
    const copied = await copyTextFallback(latestLaunchUrl);
    document.getElementById("port-url-display").textContent = latestLaunchUrl;
    document.getElementById("port-guide-box").classList.add("active");
    const btn = document.getElementById("complete-action-btn");
    btn.textContent = copied ? "LINK COPIED — READY TO PASTE (CTRL+V)" : "SHOWING LINK & STEPS";
    setTimeout(() => {
        btn.textContent = "COPY GAME LINK & SHOW STEPS";
    }, 2200);
}

function reloadCurrentGame() {
    if (busy || !selectedGameNum) return;
    launchGame(selectedGameNum);
}

function onSearchInput() {
    const input = document.getElementById("search-input");
    const clearBtn = document.getElementById("clear-search-btn");
    clearBtn.style.display = input.value.trim().length > 0 ? "inline-block" : "none";
    renderGames();
}

function clearSearch() {
    const input = document.getElementById("search-input");
    input.value = "";
    document.getElementById("clear-search-btn").style.display = "none";
    renderGames();
}

function renderGames() {
    const q = document.getElementById("search-input").value.trim().toLowerCase();
    const grid = document.getElementById("games-grid");

    const filtered = q
        ? gamesData.filter(g => g.name.toLowerCase().includes(q) || String(g.num) === q)
        : gamesData;

    if (!filtered.length) {
        grid.innerHTML = '<div class="no-results">No games found. Set GITHUB_REPO_URL at the top of codex_returns.py and click SYNC REPO.</div>';
        return;
    }

    grid.innerHTML = filtered.map((g, idx) => {
        const cacheBadge = g.cached ? '<span class="cached-tag">&bull; CACHED</span>' : 'Remote';
        const delay = Math.min(idx * 0.025, 0.45).toFixed(2);
        const fileCount = Number(g.file_count || 0);
        const sizeLabel = escapeHtml(g.size_formatted || "0 B");
        return `
            <div class="game-card" style="animation-delay: ${delay}s;">
                <div>
                    <div class="game-num">#${g.num}</div>
                    <h3>${escapeHtml(g.name)}</h3>
                    <div class="game-meta-pills">
                        <span class="meta-pill">${fileCount} ${fileCount === 1 ? "file" : "files"}</span>
                        <span class="meta-pill gold-accent">${sizeLabel}</span>
                    </div>
                </div>
                <div class="game-port">Port ${g.port} &bull; ${cacheBadge}</div>
                <button onclick="launchGame(${g.num})" ${busy ? "disabled" : ""}>LAUNCH #${g.num}</button>
            </div>
        `;
    }).join("");
}

async function loadInitialGames() {
    try {
        const res = await fetch("/api/games");
        const data = await res.json();
        if (data.quote) renderQuote(data.quote);
        if (data.active_ports) renderActivePorts(data.active_ports);
        gamesData = data.games || [];
        renderGames();
        document.getElementById("status-msg").textContent = data.status || "Ready.";
        if (!gamesData.length) {
            await syncRepo();
        }
    } catch (err) {
        document.getElementById("status-msg").textContent = "Failed to load games: " + err;
    }
}

async function syncRepo() {
    if (busy) return;
    setLoaderView(false);
    setBusyState(true, "Fetching repository folder tree...");
    try {
        const res = await fetch("/api/refresh", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.games) {
            gamesData = data.games;
            renderGames();
        }
        if (data.active_ports) {
            renderActivePorts(data.active_ports);
        }
        setBusyState(false, data.status || "Synced.");
    } catch (err) {
        setBusyState(false, "Sync error: " + err);
    }
}

function launchByNumber() {
    if (busy) return;
    const val = parseInt(document.getElementById("game-num-input").value, 10);
    if (!val || val < 1) {
        document.getElementById("status-msg").textContent = "Enter a valid game number.";
        return;
    }
    launchGame(val);
}

async function launchGame(num) {
    if (busy) return;
    selectedGameNum = num;
    latestLaunchUrl = "";
    document.getElementById("game-num-input").value = num;
    document.getElementById("ready-action-area").classList.remove("visible");

    const knownGame = gamesData.find(g => g.num === num);
    const targetPort = knownGame ? knownGame.port : (8000 + num);
    const titleText = knownGame ? `#${num} - ${knownGame.name}` : `GAME #${num}`;
    document.getElementById("launch-game-heading").innerHTML =
        `GAME LOADER // <span>${escapeHtml(titleText)}</span>`;

    setLoaderView(true);
    setBusyState(true, `Preparing Game #${num}${forceRedownload ? " (Force Redownload)" : ""}...`);
    showLaunchProgress({
        stage: "Fetching file",
        detail: forceRedownload
            ? `Force redownloading all files for Game #${num}...`
            : `Checking cache & preparing Game #${num}...`,
        percent: 1.0,
        speed: "0.0 KB/s",
        filesDone: 0,
        filesTotal: knownGame ? (knownGame.file_count || 0) : 0
    });

    const pollTimer = setInterval(async () => {
        try {
            const pRes = await fetch("/api/progress");
            const pData = await pRes.json();
            if (pData && pData.active) {
                showLaunchProgress(pData);
            }
        } catch (_) {}
    }, 110);

    try {
        const res = await fetch("/api/launch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ num: num, force_redownload: forceRedownload })
        });
        const data = await res.json();
        clearInterval(pollTimer);

        if (data.games) {
            gamesData = data.games;
        }
        if (data.active_ports) {
            renderActivePorts(data.active_ports);
        }
        if (data.ok && data.url) {
            latestLaunchUrl = data.url;
            document.getElementById("port-url-display").textContent = data.url;
            const frontCopied = await copyTextFallback(data.url);
            const isCopied = Boolean(data.copied || frontCopied);
            if (data.progress) {
                data.progress.detail = isCopied
                    ? `Ready on ${data.url} - Auto-copied to clipboard`
                    : `Ready on ${data.url} - Click button below to copy link`;
            }
        }
        if (data.progress) {
            showLaunchProgress(data.progress);
        }
        setBusyState(false, data.status || `Started Port ${targetPort}`);
        renderGames();
    } catch (err) {
        clearInterval(pollTimer);
        setBusyState(false, "Launch error: " + err);
    }
}

loadInitialGames();
</script>
</body>
</html>
"""


class LauncherState:
    def __init__(self):
        self.games: list[dict] = []
        self.quote: dict = pick_next_quote()
        self.status: str = f"Repo: {GITHUB_REPO_URL}  |  Cache: .cache/"
        self.servers: dict[int, ThreadingHTTPServer] = {}
        self.game_ports: dict[int, int] = {}
        self.port_info: dict[int, dict] = {}
        self.progress: dict = {
            "active": False,
            "stage": "Idle",
            "detail": "",
            "percent": 0.0,
            "speed": "0.0 KB/s",
            "filesDone": 0,
            "filesTotal": 0,
        }
        self.lock = threading.Lock()
        self.launch_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        GAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if INDEX_CACHE_PATH.exists():
            try:
                saved = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, list) and saved:
                    self.games = saved
                    self.status = (
                        f"Loaded {len(self.games)} games from .cache/  •  Click SYNC REPO to refresh"
                    )
            except Exception:
                pass

    def next_quote(self) -> dict:
        with self.lock:
            self.quote = pick_next_quote()
            return dict(self.quote)

    def _active_ports_locked(self) -> list[dict]:
        return [
            dict(info) for port, info in sorted(self.port_info.items()) if port in self.servers
        ]

    def get_active_ports(self) -> list[dict]:
        with self.lock:
            return self._active_ports_locked()

    def _shutdown_server_instance(self, httpd: ThreadingHTTPServer | None):
        if not httpd:
            return
        try:
            httpd.shutdown()
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
                label = f" (#{info['num']} '{info['name']}')" if info else ""
                self.status = f"Stopped Port {port}{label}"
                ok = True
            else:
                self.status = f"Port {port} was not active."
                ok = False

            snapshot = {
                "ok": ok,
                "active_ports": self._active_ports_locked(),
                "games": self._serialize_games_locked(),
                "status": self.status,
            }

        self._shutdown_server_instance(httpd)
        return snapshot

    def _set_progress(self, **kwargs):
        with self.progress_lock:
            self.progress.update(kwargs)

    def get_progress(self) -> dict:
        with self.progress_lock:
            return dict(self.progress)

    def _compute_game_stats(self, game: dict) -> tuple[int, int]:
        name = game["name"]
        files = game.get("files", [])
        sizes = game.get("file_sizes", {})
        dest_dir = GAMES_CACHE_DIR / name

        file_count = len(files)
        total_bytes = 0

        if file_count > 0:
            for rel in files:
                local_file = dest_dir / rel
                if local_file.exists() and local_file.is_file():
                    total_bytes += local_file.stat().st_size
                elif isinstance(sizes, dict) and isinstance(sizes.get(rel), (int, float)):
                    total_bytes += int(sizes[rel])
        elif dest_dir.exists():
            disk_files = [p for p in dest_dir.rglob("*") if p.is_file()]
            file_count = len(disk_files)
            total_bytes = sum(p.stat().st_size for p in disk_files)

        return file_count, total_bytes

    def _serialize_games_locked(self) -> list[dict]:
        out = []
        for g in self.games:
            num = g["num"]
            name = g["name"]
            file_count, total_bytes = self._compute_game_stats(g)
            out.append(
                {
                    "num": num,
                    "name": name,
                    "port": self.game_ports.get(num, BASE_PORT + num),
                    "cached": (GAMES_CACHE_DIR / name / "index.html").exists(),
                    "file_count": file_count,
                    "total_bytes": total_bytes,
                    "size_formatted": format_size(total_bytes),
                }
            )
        return out

    def serialize_games(self) -> list[dict]:
        with self.lock:
            return self._serialize_games_locked()

    def refresh_games(self, token: str = "") -> dict:
        with self.launch_lock:
            try:
                owner, repo = parse_github_repo(GITHUB_REPO_URL)
                session = build_session(token or os.environ.get("GITHUB_TOKEN", ""))
                tree_data = None
                used_branch = BRANCH

                for b in [BRANCH, "master" if BRANCH != "master" else "main"]:
                    url = (
                        f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
                        f"/git/trees/{quote(b)}?recursive=1"
                    )
                    resp = session.get(url, timeout=30)
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    tree_data = resp.json()
                    used_branch = b
                    break

                if not tree_data or "tree" not in tree_data:
                    raise RuntimeError("Could not fetch repository tree on 'main' or 'master'.")

                prefix = GAMES_FOLDER.strip("/") + "/"
                folders: set[str] = set()
                file_map: dict[str, list[str]] = {}
                size_map: dict[str, dict[str, int]] = {}

                for item in tree_data["tree"]:
                    path = item.get("path", "")
                    if not path.startswith(prefix):
                        continue
                    rel = path[len(prefix):]
                    parts = rel.split("/")
                    game_name = parts[0].strip()
                    if not game_name:
                        continue
                    folders.add(game_name)
                    if item.get("type") == "blob" and len(parts) >= 2:
                        sub_rel = "/".join(parts[1:])
                        file_map.setdefault(game_name, []).append(sub_rel)
                        if isinstance(item.get("size"), int):
                            size_map.setdefault(game_name, {})[sub_rel] = item["size"]

                new_games = [
                    {
                        "num": idx,
                        "name": name,
                        "branch": used_branch,
                        "files": sorted(file_map.get(name, [])),
                        "file_sizes": size_map.get(name, {}),
                    }
                    for idx, name in enumerate(sorted(folders, key=str.lower), start=1)
                ]
                INDEX_CACHE_PATH.write_text(json.dumps(new_games, indent=2), encoding="utf-8")

                with self.lock:
                    self.games = new_games
                    self.status = (
                        f"Found {len(self.games)} games in '{GAMES_FOLDER}/' ({used_branch}) • Cached in .cache/"
                    )
                    return {
                        "ok": True,
                        "games": self._serialize_games_locked(),
                        "active_ports": self._active_ports_locked(),
                        "status": self.status,
                    }
            except Exception as exc:
                with self.lock:
                    self.status = f"Error: {exc}"
                    return {
                        "ok": False,
                        "games": self._serialize_games_locked(),
                        "active_ports": self._active_ports_locked(),
                        "status": self.status,
                    }

    def _collect_files_recursive(
        self, session: requests.Session, owner: str, repo: str, branch: str, folder_rel: str
    ) -> tuple[list[str], dict[str, int]]:
        found: list[str] = []
        sizes: dict[str, int] = {}
        base_folder = f"{GAMES_FOLDER.strip('/')}/{folder_rel}".strip("/")

        def walk(sub_rel: str):
            target_path = f"{base_folder}/{sub_rel}".strip("/") if sub_rel else base_folder
            api_url = (
                f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
                f"/contents/{quote(target_path, safe='/')}?ref={quote(branch)}"
            )
            r = session.get(api_url, timeout=20)
            r.raise_for_status()
            entries = r.json()
            if not isinstance(entries, list):
                return
            for entry in entries:
                etype = entry.get("type")
                ename = entry.get("name", "")
                if not ename:
                    continue
                next_rel = f"{sub_rel}/{ename}".strip("/") if sub_rel else ename
                if etype == "file":
                    found.append(next_rel)
                    if isinstance(entry.get("size"), int):
                        sizes[next_rel] = entry["size"]
                elif etype == "dir":
                    walk(next_rel)

        walk("")
        return sorted(found), sizes

    def launch_game(self, num: int, force_redownload: bool = False, token: str = "") -> dict:
        with self.launch_lock:
            start_time = time.monotonic()
            total_bytes_downloaded = 0
            completed_files = 0
            skipped_files = 0
            total_files = 0
            desired_port = BASE_PORT + num

            self._set_progress(
                active=True,
                stage="Fetching file",
                detail=f"Resolving Game #{num} metadata...",
                percent=2.0,
                speed="0.0 KB/s",
                filesDone=0,
                filesTotal=0,
            )

            try:
                with self.lock:
                    game = next((g for g in self.games if g["num"] == num), None)
                if not game:
                    raise ValueError(f"Game #{num} not found in list.")

                owner, repo = parse_github_repo(GITHUB_REPO_URL)
                session = build_session(token or os.environ.get("GITHUB_TOKEN", ""))
                name = game["name"]
                branch = game.get("branch", BRANCH)
                files = list(game.get("files", []))
                expected_sizes = dict(game.get("file_sizes") or {})
                dest_dir = GAMES_CACHE_DIR / name
                dest_dir.mkdir(parents=True, exist_ok=True)
                resolved_dest = dest_dir.resolve()

                if not files:
                    self._set_progress(
                        active=True,
                        stage="Fetching file",
                        detail=f"Scanning directory tree for '{name}'...",
                        percent=6.0,
                        speed="0.0 KB/s",
                        filesDone=0,
                        filesTotal=0,
                    )
                    files, discovered_sizes = self._collect_files_recursive(
                        session, owner, repo, branch, name
                    )
                    game["files"] = files
                    game["file_sizes"] = discovered_sizes
                    expected_sizes = discovered_sizes

                total_files = len(files)
                if total_files == 0:
                    raise FileNotFoundError(f"No files found inside '{GAMES_FOLDER}/{name}/'.")

                progress_io_lock = threading.Lock()

                def fetch_one(rel_path: str):
                    nonlocal total_bytes_downloaded, completed_files, skipped_files
                    out_file = (dest_dir / rel_path).resolve()
                    if not out_file.is_relative_to(resolved_dest):
                        raise ValueError(f"Invalid relative file path: {rel_path}")
                    expected_sz = expected_sizes.get(rel_path)

                    if not force_redownload and out_file.exists() and out_file.is_file():
                        existing_bytes = out_file.read_bytes()
                        is_lfs = _parse_lfs_pointer(existing_bytes) is not None
                        actual_sz = len(existing_bytes)
                        if not is_lfs and (
                            (expected_sz is None and actual_sz > 0)
                            or (isinstance(expected_sz, int) and actual_sz == expected_sz)
                        ):
                            with progress_io_lock:
                                completed_files += 1
                                skipped_files += 1
                                elapsed = max(time.monotonic() - start_time, 0.001)
                                speed_str = format_speed(total_bytes_downloaded / elapsed)
                                pct = min(94.0, 8.0 + (completed_files / max(total_files, 1)) * 86.0)
                            self._set_progress(
                                active=True,
                                stage="Fetching file",
                                detail=f"Cached (skipped): {rel_path}",
                                percent=pct,
                                speed=speed_str,
                                filesDone=completed_files,
                                filesTotal=total_files,
                            )
                            return

                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    encoded_rel = "/".join(quote(p) for p in rel_path.split("/"))
                    raw_url = (
                        f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}"
                        f"/{quote(branch)}/{quote(GAMES_FOLDER.strip('/'))}/{quote(name)}/{encoded_rel}"
                    )

                    self._set_progress(
                        active=True,
                        stage="Fetching file",
                        detail=f"Fetching file: {rel_path}",
                    )

                    with session.get(raw_url, stream=True, timeout=30) as resp:
                        resp.raise_for_status()
                        chunks: list[bytes] = []
                        for chunk in resp.iter_content(chunk_size=32768):
                            if not chunk:
                                continue
                            chunks.append(chunk)
                            c_len = len(chunk)
                            with progress_io_lock:
                                total_bytes_downloaded += c_len
                                elapsed = max(time.monotonic() - start_time, 0.001)
                                speed_str = format_speed(total_bytes_downloaded / elapsed)
                                file_ratio = (completed_files + 0.5) / max(total_files, 1)
                                pct = min(92.0, 8.0 + file_ratio * 84.0)
                            self._set_progress(
                                active=True,
                                stage="Fetching file",
                                detail=f"Fetching file: {rel_path}",
                                percent=pct,
                                speed=speed_str,
                                filesDone=completed_files,
                                filesTotal=total_files,
                            )

                    raw_bytes = b"".join(chunks)
                    lfs_ptr = _parse_lfs_pointer(raw_bytes)
                    if lfs_ptr is not None:
                        oid, lfs_size = lfs_ptr
                        self._set_progress(
                            active=True,
                            stage="Fetching file",
                            detail=f"Resolving Git LFS object: {rel_path}",
                        )
                        lfs_url = _resolve_lfs_download_url(session, owner, repo, oid, lfs_size)
                        with session.get(lfs_url, stream=True, timeout=60) as lfs_resp:
                            lfs_resp.raise_for_status()
                            lfs_chunks: list[bytes] = []
                            for lfs_chunk in lfs_resp.iter_content(chunk_size=65536):
                                if not lfs_chunk:
                                    continue
                                lfs_chunks.append(lfs_chunk)
                                c_len = len(lfs_chunk)
                                with progress_io_lock:
                                    total_bytes_downloaded += c_len
                                    elapsed = max(time.monotonic() - start_time, 0.001)
                                    speed_str = format_speed(total_bytes_downloaded / elapsed)
                                    file_ratio = (completed_files + 0.5) / max(total_files, 1)
                                    pct = min(92.0, 8.0 + file_ratio * 84.0)
                                self._set_progress(
                                    active=True,
                                    stage="Fetching file",
                                    detail=f"Fetching LFS file: {rel_path}",
                                    percent=pct,
                                    speed=speed_str,
                                    filesDone=completed_files,
                                    filesTotal=total_files,
                                )
                        raw_bytes = b"".join(lfs_chunks)

                    out_file.write_bytes(raw_bytes)
                    with progress_io_lock:
                        completed_files += 1
                        elapsed = max(time.monotonic() - start_time, 0.001)
                        speed_str = format_speed(total_bytes_downloaded / elapsed)
                        pct = min(94.0, 8.0 + (completed_files / max(total_files, 1)) * 86.0)
                    self._set_progress(
                        active=True,
                        stage="Fetching file",
                        detail=f"Fetched {rel_path}",
                        percent=pct,
                        speed=speed_str,
                        filesDone=completed_files,
                        filesTotal=total_files,
                    )

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = [pool.submit(fetch_one, rel) for rel in files]
                    for fut in as_completed(futures):
                        fut.result()

                elapsed = max(time.monotonic() - start_time, 0.001)
                speed_str = format_speed(total_bytes_downloaded / elapsed)
                self._set_progress(
                    active=True,
                    stage="Building files",
                    detail=f"Building files & verifying index.html ({skipped_files} cached skipped)...",
                    percent=97.0,
                    speed=speed_str,
                    filesDone=completed_files,
                    filesTotal=total_files,
                )

                index_path = dest_dir / "index.html"
                if not index_path.exists():
                    raise FileNotFoundError(f"index.html not found in .cache/games/{name}/")

                old_httpd = None
                with self.lock:
                    if num in self.game_ports and self.game_ports[num] in self.servers:
                        old_port = self.game_ports[num]
                        old_httpd = self.servers.pop(old_port, None)
                        self.port_info.pop(old_port, None)
                        self.game_ports.pop(num, None)
                self._shutdown_server_instance(old_httpd)

                actual_port, exact_match = find_free_port(desired_port)
                self._set_progress(
                    active=True,
                    stage=f"Starting Port {actual_port}",
                    detail=(
                        f"Starting Port {actual_port} for '{name}'..."
                        if exact_match
                        else f"Port {desired_port} busy - Starting Port {actual_port} for '{name}'..."
                    ),
                    percent=99.0,
                    speed=speed_str,
                    filesDone=completed_files,
                    filesTotal=total_files,
                )

                handler = partial(GameBootHTTPHandler, directory=str(dest_dir))
                httpd = ThreadingHTTPServer(("127.0.0.1", actual_port), handler)
                threading.Thread(target=httpd.serve_forever, daemon=True).start()

                url = f"http://127.0.0.1:{actual_port}/index.html"
                copied = copy_to_clipboard(url)
                clip_note = "Auto-copied to clipboard" if copied else "Copy link below"

                with self.lock:
                    self.servers[actual_port] = httpd
                    self.game_ports[num] = actual_port
                    self.port_info[actual_port] = {
                        "port": actual_port,
                        "num": num,
                        "name": name,
                        "url": url,
                    }
                    self.status = f"Port {actual_port} active for #{num} '{name}' • {clip_note}"
                    games_snap = self._serialize_games_locked()
                    ports_snap = self._active_ports_locked()

                final_progress = {
                    "active": True,
                    "stage": f"Starting Port {actual_port}",
                    "detail": f"Ready on {url} - {clip_note}",
                    "percent": 100.0,
                    "speed": speed_str,
                    "filesDone": completed_files,
                    "filesTotal": total_files,
                }
                self._set_progress(**final_progress)

                return {
                    "ok": True,
                    "url": url,
                    "port": actual_port,
                    "copied": copied,
                    "progress": final_progress,
                    "games": games_snap,
                    "active_ports": ports_snap,
                    "status": self.status,
                }
            except Exception as exc:
                elapsed = max(time.monotonic() - start_time, 0.001)
                speed_str = format_speed(total_bytes_downloaded / elapsed)
                err_progress = {
                    "active": True,
                    "stage": "Launch failed",
                    "detail": str(exc),
                    "percent": 0.0,
                    "speed": speed_str,
                    "filesDone": completed_files,
                    "filesTotal": total_files,
                }
                self._set_progress(**err_progress)
                with self.lock:
                    self.status = f"Launch failed: {exc}"
                    games_snap = self._serialize_games_locked()
                    ports_snap = self._active_ports_locked()
                return {
                    "ok": False,
                    "progress": err_progress,
                    "games": games_snap,
                    "active_ports": ports_snap,
                    "status": self.status,
                }

    def stop_all(self):
        with self.lock:
            instances = list(self.servers.values())
            self.servers.clear()
            self.game_ports.clear()
            self.port_info.clear()
        for httpd in instances:
            self._shutdown_server_instance(httpd)


def make_ui_handler(state: LauncherState):
    class UIHandler(QuietUIHandler):
        def _send_json(self, payload: dict, code: int = 200):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/games":
                with state.lock:
                    quote_snap = dict(state.quote)
                    status_snap = state.status
                self._send_json(
                    {
                        "games": state.serialize_games(),
                        "active_ports": state.get_active_ports(),
                        "status": status_snap,
                        "quote": quote_snap,
                    }
                )
                return
            if parsed.path == "/api/quote":
                self._send_json({"quote": state.next_quote()})
                return
            if parsed.path == "/api/progress":
                self._send_json(state.get_progress())
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                body = json.loads(raw_body) if raw_body else {}
            except Exception:
                body = {}

            if parsed.path == "/api/refresh":
                self._send_json(state.refresh_games())
                return
            if parsed.path == "/api/launch":
                num = int(body.get("num", 0))
                force_flag = bool(body.get("force_redownload", False))
                self._send_json(state.launch_game(num, force_redownload=force_flag))
                return
            if parsed.path == "/api/stop_port":
                port = int(body.get("port", 0))
                self._send_json(state.stop_port(port))
                return
            self.send_response(404)
            self.end_headers()

    return UIHandler


if __name__ == "__main__":
    hide_console_window()
    state = LauncherState()
    ui_port, _ = find_free_port(None)
    ui_server = ThreadingHTTPServer(("127.0.0.1", ui_port), make_ui_handler(state))
    threading.Thread(target=ui_server.serve_forever, daemon=True).start()

    window = webview.create_window(
        title="Ali's Productions presents CodeX-Returns",
        url=f"http://127.0.0.1:{ui_port}/",
        width=1366,
        height=820,
        min_size=(860, 600),
        maximized=True,
        background_color="#000000",
    )
    try:
        webview.start(func=lambda: maximize_app_window(window))
    finally:
        state.stop_all()
        ui_server.shutdown()
        ui_server.server_close()