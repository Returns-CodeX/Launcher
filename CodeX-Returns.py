import ctypes
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import webview

# ==============================================================================
# 1. REPOSITORY & LAUNCHER CONFIGURATION
# ==============================================================================
GITHUB_REPO_URL = "https://github.com/Returns-CodeX/CodeX-files"
BRANCH = "main"          # Automatically tries "master" if "main" returns 404
GAMES_FOLDER = "files"   # Searches for folders inside "<repo>/files/<game_name>/"
BASE_PORT = 8000         # Game #12 -> http://127.0.0.1:8012/index.html
MAX_WORKERS = 24         # Parallel downloads for repos with 1,000+ games
WINDOW_TITLE = "Ali's Productions presents CodeX-Returns"

# ==============================================================================
# 2. CUSTOM QUOTES (ADD MORE TUPLES ANYTIME)
# ==============================================================================
QUOTES = [
    (
        '"It is fine to get hurt\n'
        ' wounds always heal\n'
        ' anyways, chicks dig scars."',
        "ALI",
    ),
    (
        '"I don\'t hold grudges,\n'
        ' I just remember\n'
        ' who was there."',
        "ALI",
    ),
    (
        '"I don\'t need\n'
        ' a comeback,\n'
        ' I never left and never will."',
        "ALI",
    ),
    (
        '"Scars are just,\n'
        ' proof I showed up\n'
        ' and survived."',
        "ALI",
    ),
]

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
GAMES_CACHE_DIR = CACHE_DIR / "games"
INDEX_CACHE_PATH = CACHE_DIR / "games_index.json"
LAST_QUOTE_PATH = CACHE_DIR / "last_quote.json"
USER_STATE_PATH = CACHE_DIR / "launcher_state.json"

DEFAULT_CONFIG = {
    "repo_url": GITHUB_REPO_URL,
    "branch": BRANCH,
    "games_folder": GAMES_FOLDER,
    "max_workers": MAX_WORKERS,
    "github_token": "",
    "show_boot_overlay": True,
    "bg_interaction_mode": "repel",
    "bg_density": 84,
}


def build_boot_snippet(enabled: bool = True) -> str:
    if not enabled:
        return ""
    return """
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
    """Maximize the desktop window while keeping title bar controls."""
    try:
        win.maximize()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
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
        LAST_QUOTE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        "Set a valid GitHub repository URL in Settings or at the top of codex_returns.py."
    )


def build_session(token: str = "", pool_size: int = MAX_WORKERS) -> requests.Session:
    workers = max(1, min(64, int(pool_size or MAX_WORKERS)))
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "CodeX-Returns-Launcher/11.0",
            "Accept": "application/vnd.github+json",
        }
    )
    if token.strip():
        session.headers["Authorization"] = f"Bearer {token.strip()}"
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=workers, pool_maxsize=workers
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


def format_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    hrs, rem = divmod(sec, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def get_dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total


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
            for tool in (
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ):
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


def get_battery_status() -> dict:
    """Return battery percentage, charging flag, and label across platforms."""
    try:
        import psutil  # type: ignore

        bat = psutil.sensors_battery()
        if bat is not None:
            pct = max(0, min(100, int(round(bat.percent))))
            charging = bool(bat.power_plugged)
            label = f"{pct}% {'(AC)' if charging else ''}".strip()
            return {"percent": pct, "charging": charging, "available": True, "label": label}
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                pct_raw = status.BatteryLifePercent
                charging = status.ACLineStatus == 1
                if pct_raw <= 100:
                    pct = int(pct_raw)
                    return {
                        "percent": pct,
                        "charging": charging,
                        "available": True,
                        "label": f"{pct}% {'(AC)' if charging else ''}".strip(),
                    }
                if charging:
                    return {
                        "percent": 100,
                        "charging": True,
                        "available": True,
                        "label": "AC Power",
                    }
        except Exception:
            pass
    else:
        power_root = Path("/sys/class/power_supply")
        if power_root.exists():
            try:
                for supply in power_root.iterdir():
                    stype = (supply / "type").read_text(errors="ignore").strip().lower()
                    if "battery" in stype:
                        cap_path = supply / "capacity"
                        status_path = supply / "status"
                        if cap_path.exists():
                            pct = max(0, min(100, int(cap_path.read_text(errors="ignore").strip())))
                            st_text = (
                                status_path.read_text(errors="ignore").strip().lower()
                                if status_path.exists()
                                else ""
                            )
                            charging = st_text in ("charging", "full")
                            return {
                                "percent": pct,
                                "charging": charging,
                                "available": True,
                                "label": f"{pct}% {'(AC)' if charging else ''}".strip(),
                            }
            except Exception:
                pass

    return {"percent": None, "charging": False, "available": False, "label": "AC / Desktop"}


def get_system_stats(
    active_servers_count: int = 0,
    cached_games_count: int = 0,
    launcher_uptime_sec: float = 0.0,
) -> dict:
    """Gather computer & runtime telemetry for the System Stats page."""
    now = datetime.now()
    bat = get_battery_status()

    cpu_percent = None
    cpu_cores_logical = os.cpu_count() or 1
    ram_used_str = "N/A"
    ram_total_str = "N/A"
    ram_percent = None
    proc_mem_str = "N/A"
    proc_threads = 0

    try:
        import psutil  # type: ignore

        cpu_percent = round(float(psutil.cpu_percent(interval=0.05)), 1)
        cpu_cores_logical = psutil.cpu_count(logical=True) or cpu_cores_logical
        vm = psutil.virtual_memory()
        ram_percent = round(float(vm.percent), 1)
        ram_used_str = format_size(int(vm.used))
        ram_total_str = format_size(int(vm.total))
        cur_proc = psutil.Process(os.getpid())
        proc_mem_str = format_size(int(cur_proc.memory_info().rss))
        proc_threads = int(cur_proc.num_threads())
    except Exception:
        proc_threads = threading.active_count()
        if sys.platform == "win32":
            try:
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                    total = int(mem.ullTotalPhys)
                    avail = int(mem.ullAvailPhys)
                    used = max(0, total - avail)
                    ram_percent = round((used / total) * 100, 1) if total else 0.0
                    ram_used_str = format_size(used)
                    ram_total_str = format_size(total)
            except Exception:
                pass
        elif Path("/proc/meminfo").exists():
            try:
                meminfo = {}
                for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        nums = re.findall(r"\d+", v)
                        if nums:
                            meminfo[k.strip()] = int(nums[0]) * 1024
                total = meminfo.get("MemTotal", 0)
                avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
                if total > 0:
                    used = max(0, total - avail)
                    ram_percent = round((used / total) * 100, 1)
                    ram_used_str = format_size(used)
                    ram_total_str = format_size(total)
            except Exception:
                pass

    disk_free_str = "N/A"
    disk_total_str = "N/A"
    disk_percent = None
    try:
        du = shutil.disk_usage(BASE_DIR)
        used_d = max(0, du.total - du.free)
        disk_free_str = format_size(du.free)
        disk_total_str = format_size(du.total)
        disk_percent = round((used_d / du.total) * 100, 1) if du.total else 0.0
    except Exception:
        pass

    cache_bytes = get_dir_size_bytes(CACHE_DIR)
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.25)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except Exception:
        pass

    return {
        "time": now.strftime("%I:%M:%S %p"),
        "date": now.strftime("%a, %b %d, %Y"),
        "battery": bat,
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine() or "Unknown",
        "python": platform.python_version(),
        "hostname": hostname,
        "local_ip": local_ip,
        "cpu_percent": cpu_percent,
        "cpu_cores": cpu_cores_logical,
        "ram_percent": ram_percent,
        "ram_used": ram_used_str,
        "ram_total": ram_total_str,
        "disk_percent": disk_percent,
        "disk_free": disk_free_str,
        "disk_total": disk_total_str,
        "cache_size": format_size(cache_bytes),
        "cached_games": cached_games_count,
        "active_servers": active_servers_count,
        "proc_mem": proc_mem_str,
        "proc_threads": proc_threads,
        "pid": os.getpid(),
        "uptime": format_duration(launcher_uptime_sec),
    }


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
    boot_enabled = True

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
                snippet = build_boot_snippet(self.boot_enabled)
                if snippet and "codex-browser-boot" not in raw:
                    if "<body" in raw.lower():
                        raw = re.sub(
                            r"(<body[^>]*>)",
                            r"\1" + snippet,
                            raw,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                    else:
                        raw = snippet + raw
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
    --dark: #070708;
    --darker: #000000;
    --panel: rgba(11, 10, 12, 0.9);
    --text: #f4f4f6;
    --text-muted: #98949c;
    --glow-red: 0 0 18px rgba(255, 0, 34, 0.42);
    --glow-red-strong: 0 0 28px rgba(255, 0, 34, 0.65);
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
    line-height: 1.5;
}

h1, h2, h3, h4, h5, h6 {
    font-family: 'Audiowide', cursive;
    font-weight: 400;
    letter-spacing: 1.2px;
}

::-webkit-scrollbar {
    width: 7px;
    height: 7px;
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
    50% { transform: scale(1.02); }
}

@keyframes fadeSlideIn {
    from { opacity: 0; transform: translateY(10px) scale(0.992); }
    to { opacity: 1; transform: translateY(0) scale(1); }
}

@keyframes bootSweep {
    0% { left: -45%; }
    100% { left: 115%; }
}

@keyframes shimmerBar {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}

#bg-canvas {
    position: fixed;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 0;
    pointer-events: none;
}

.bg-grid {
    position: fixed;
    inset: 0;
    background-image:
        linear-gradient(rgba(255, 0, 34, 0.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 0, 34, 0.045) 1px, transparent 1px);
    background-size: 34px 34px;
    pointer-events: none;
    z-index: 1;
}

/* Splash Screen */
#splash {
    position: fixed;
    inset: 0;
    z-index: 70;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    background: radial-gradient(circle at center, rgba(255, 0, 34, 0.2), #000000 82%);
    transition: opacity 0.45s ease, transform 0.45s ease;
}

#splash.fade-out {
    opacity: 0;
    transform: scale(1.03);
    pointer-events: none;
}

.console-splash-shell {
    width: 100%;
    max-width: 760px;
    background: linear-gradient(155deg, rgba(16, 8, 10, 0.97), rgba(3, 3, 4, 0.99));
    border: 1px solid rgba(255, 0, 34, 0.48);
    border-radius: 6px;
    padding: 2.5rem 2.3rem;
    position: relative;
    overflow: hidden;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.95), var(--glow-red-strong);
    animation: fadeSlideIn 0.4s ease-out;
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
    font-size: 0.74rem;
    letter-spacing: 2px;
    color: var(--primary-light);
    margin-bottom: 1.3rem;
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
    font-size: 0.96rem;
    font-weight: 700;
    letter-spacing: 3.5px;
    text-transform: uppercase;
    color: var(--gold);
    margin-bottom: 0.35rem;
}

.splash-console-title {
    text-align: center;
    font-size: clamp(2.1rem, 5vw, 3.5rem);
    color: var(--primary);
    text-shadow: 0 0 14px rgba(255, 0, 34, 0.85);
    animation: pulse 2.4s infinite ease-in-out;
    margin-bottom: 0.35rem;
}

.splash-console-sub {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.96rem;
    letter-spacing: 2px;
    margin-bottom: 1.5rem;
}

.boot-diag-box {
    background: rgba(0, 0, 0, 0.82);
    border: 1px solid rgba(255, 0, 34, 0.24);
    border-radius: 5px;
    padding: 0.95rem 1.2rem;
    margin-bottom: 1.3rem;
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
    font-size: 0.75rem;
}

.boot-diag-line span.ok {
    color: var(--primary-light);
    font-weight: 700;
    letter-spacing: 1px;
}

.boot-gauge-wrap {
    margin-bottom: 1.15rem;
}

.boot-gauge-meta {
    display: flex;
    justify-content: space-between;
    font-family: 'Audiowide', cursive;
    font-size: 0.8rem;
    margin-bottom: 0.45rem;
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

/* Top Navigation Bar (Sole Navigation Chrome) */
.top-nav {
    position: sticky;
    top: 0;
    z-index: 45;
    background: rgba(5, 5, 6, 0.94);
    backdrop-filter: blur(14px);
    border-bottom: 1px solid rgba(255, 0, 34, 0.34);
    box-shadow: 0 6px 26px rgba(0, 0, 0, 0.88), var(--glow-red);
    padding: 0.65rem 1.4rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
}

.top-nav-left {
    display: flex;
    align-items: center;
    gap: 0.85rem;
}

.brand-block {
    display: flex;
    flex-direction: column;
    text-align: left;
}

.brand-presenter {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 2.6px;
    text-transform: uppercase;
    color: var(--gold);
    line-height: 1.1;
}

.brand-title {
    font-size: 1.35rem;
    color: var(--primary);
    text-shadow: 0 0 12px rgba(255, 0, 34, 0.75);
    line-height: 1.15;
}

.top-nav-tabs {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
}

.nav-tab-btn {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 0, 34, 0.25);
    color: var(--text-muted);
    padding: 0.46rem 0.92rem;
    border-radius: 4px;
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all 0.22s ease;
}

.nav-tab-btn:hover {
    color: var(--text);
    border-color: rgba(255, 0, 34, 0.6);
    background: rgba(255, 0, 34, 0.14);
}

.nav-tab-btn.active {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border-color: var(--primary-light);
    box-shadow: var(--glow-red);
}

.top-nav-telemetry {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    flex-wrap: wrap;
}

.telem-pill {
    background: rgba(14, 7, 9, 0.92);
    border: 1px solid rgba(255, 0, 34, 0.36);
    border-radius: 4px;
    padding: 0.34rem 0.75rem;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    color: var(--text);
}

.telem-pill .telem-key {
    color: var(--text-muted);
    font-size: 0.65rem;
    letter-spacing: 1px;
}

.telem-pill .accent-gold {
    color: var(--gold);
}

.telem-pill .accent-green {
    color: var(--green);
}

/* Main Workspace */
.workspace-shell {
    position: relative;
    z-index: 5;
    display: flex;
    flex: 1;
    width: 100%;
    max-width: 1520px;
    margin: 0 auto;
}

main.page-host {
    flex: 1;
    min-width: 0;
    padding: 1.35rem 1.8rem 2.4rem;
    display: flex;
    flex-direction: column;
    gap: 1.15rem;
}

/* Active Ports Strip */
.active-ports-bar {
    width: 100%;
    background: linear-gradient(135deg, rgba(16, 7, 9, 0.94), rgba(5, 5, 6, 0.97));
    border: 1px solid rgba(255, 0, 34, 0.36);
    border-radius: 6px;
    padding: 0.68rem 1.15rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.9rem;
    flex-wrap: wrap;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.78);
}

.active-ports-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.76rem;
    color: var(--primary-light);
    letter-spacing: 1.3px;
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
    gap: 0.55rem;
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
    padding: 0.32rem 0.68rem;
    font-family: 'Rajdhani', sans-serif;
    font-size: 0.92rem;
    font-weight: 700;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
}

.port-pill-copy strong {
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
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
    padding: 0.32rem 0.58rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.66rem;
    cursor: pointer;
}

.port-pill-stop:hover {
    background: var(--primary);
}

/* Pages */
.page-view {
    display: none;
    width: 100%;
    animation: fadeSlideIn 0.32s cubic-bezier(0.22, 1, 0.36, 1);
}

.page-view.active {
    display: block;
}

.surface-panel {
    width: 100%;
    background: var(--panel);
    backdrop-filter: blur(12px);
    padding: 1.75rem;
    border-radius: 6px;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.85), var(--glow-red);
    border: 1px solid rgba(255, 0, 34, 0.28);
    position: relative;
    overflow: hidden;
    margin-bottom: 1.15rem;
}

.surface-panel::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 3px;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
}

.section-heading-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.1rem;
}

.section-heading {
    font-size: 1.12rem;
    color: var(--text);
}

.section-heading span {
    color: var(--gold);
}

/* Quote Banner */
.quote-banner {
    width: 100%;
    background: linear-gradient(135deg, rgba(16, 7, 9, 0.95), rgba(6, 6, 7, 0.97));
    border: 1px solid rgba(255, 0, 34, 0.34);
    border-radius: 6px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1.15rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    position: relative;
    overflow: hidden;
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
    font-size: 1.24rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1.45;
}

.quote-line {
    display: block;
}

.quote-footer-bar {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1rem;
    margin-top: 0.6rem;
    flex-wrap: wrap;
}

.quote-author {
    font-family: 'Audiowide', cursive;
    font-size: 0.84rem;
    color: var(--gold);
    letter-spacing: 2.5px;
}

.cycle-quote-btn {
    background: rgba(255, 0, 34, 0.14);
    color: var(--primary-light);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 4px;
    padding: 0.32rem 0.8rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.68rem;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all 0.2s ease;
}

.cycle-quote-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #ffffff;
    box-shadow: var(--glow-red);
}

/* Home Featured Grid */
.featured-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(235px, 1fr));
    gap: 1rem;
}

.featured-card {
    background: linear-gradient(160deg, rgba(22, 9, 12, 0.94), rgba(6, 6, 8, 0.98));
    border: 1px solid rgba(255, 0, 34, 0.34);
    border-radius: 6px;
    padding: 1.1rem;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 0.8rem;
    transition: transform 0.24s ease, border-color 0.24s ease, box-shadow 0.24s ease;
}

.featured-card:hover {
    transform: translateY(-4px);
    border-color: var(--primary);
    box-shadow: 0 12px 24px rgba(0, 0, 0, 0.85), var(--glow-red);
}

.featured-num {
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    color: var(--gold);
    letter-spacing: 1.5px;
}

.featured-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.95rem;
    color: var(--text);
    word-break: break-word;
}

.featured-meta {
    font-size: 0.88rem;
    color: var(--text-muted);
}

/* Controls & Buttons */
.controls-top {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.85rem;
    margin-bottom: 1rem;
}

.controls-group {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.65rem;
}

.action-btn {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(255, 0, 34, 0.65);
    padding: 0.68rem 1.25rem;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
    font-family: 'Audiowide', cursive;
    letter-spacing: 1px;
    font-size: 0.76rem;
    transition: all 0.22s ease;
    box-shadow: 0 6px 18px rgba(255, 0, 34, 0.35);
}

.action-btn:hover:not(:disabled) {
    background: linear-gradient(135deg, var(--primary), var(--primary-light));
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(255, 0, 34, 0.62);
}

.action-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.action-btn.compact {
    padding: 0.44rem 0.82rem;
    font-size: 0.68rem;
}

.secondary-btn {
    background: rgba(255, 0, 34, 0.08);
    color: var(--text);
    border: 1px solid rgba(255, 0, 34, 0.42);
    box-shadow: none;
}

.secondary-btn:hover:not(:disabled) {
    background: rgba(255, 0, 34, 0.24);
    border-color: var(--primary);
    box-shadow: var(--glow-red);
}

.controls-sub {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.85rem;
    flex-wrap: wrap;
    margin-bottom: 1.2rem;
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
    flex: 1;
    min-width: 240px;
    max-width: 540px;
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
    font-size: 1.06rem;
    width: 100%;
    padding: 0.3rem 0;
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
    font-size: 0.72rem;
    padding: 0.25rem 0.45rem;
    display: none;
}

.clear-search-btn:hover {
    color: var(--primary);
}

.sort-select, .form-input, .form-select {
    background: rgba(0, 0, 0, 0.88);
    border: 1px solid rgba(255, 0, 34, 0.36);
    border-radius: 4px;
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    font-size: 1rem;
    font-weight: 600;
    padding: 0.46rem 0.75rem;
}

.sort-select:focus, .form-input:focus, .form-select:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: var(--glow-red);
}

/* Favorites Shelf on Games Page */
.favorites-shelf {
    display: none;
    margin-bottom: 1.4rem;
    padding: 1.1rem 1.2rem;
    border-radius: 6px;
    background: linear-gradient(155deg, rgba(26, 16, 6, 0.72), rgba(8, 6, 7, 0.95));
    border: 1px solid rgba(212, 175, 55, 0.48);
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.75);
}

.favorites-shelf.visible {
    display: block;
    animation: fadeSlideIn 0.25s ease-out;
}

.favorites-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.85rem;
}

.favorites-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.92rem;
    color: var(--gold);
    letter-spacing: 1.4px;
}

.favorites-count {
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    color: var(--text-muted);
}

/* Focused Launch Panel */
.launch-panel {
    display: none;
    width: 100%;
    max-width: 900px;
    margin: 0 auto 1.3rem;
    padding: 1.45rem 1.6rem;
    background: linear-gradient(155deg, rgba(16, 8, 10, 0.98), rgba(3, 3, 4, 0.99));
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 6px;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.9), var(--glow-red);
    animation: fadeSlideIn 0.25s ease-out;
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
    padding-bottom: 0.8rem;
    margin-bottom: 0.9rem;
    border-bottom: 1px solid rgba(255, 0, 34, 0.22);
}

.launch-game-heading {
    font-family: 'Audiowide', cursive;
    font-size: 1.08rem;
}

.launch-game-heading span {
    color: var(--gold);
}

.launch-page-controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
}

.force-toggle-btn {
    background: rgba(255, 255, 255, 0.03);
    color: var(--text-muted);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 4px;
    padding: 0.34rem 0.75rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.68rem;
    cursor: pointer;
}

.force-toggle-btn.active {
    background: rgba(255, 0, 34, 0.22);
    color: #ffffff;
    border-color: var(--primary);
}

.back-catalog-btn {
    background: rgba(255, 0, 34, 0.12);
    color: var(--text);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 4px;
    padding: 0.34rem 0.8rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.68rem;
    cursor: pointer;
}

.back-catalog-btn:hover {
    background: var(--primary);
    color: #ffffff;
}

.launch-stage-line {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 0.3rem;
}

.launch-file-line {
    font-size: 0.92rem;
    color: var(--text-muted);
    margin-bottom: 0.9rem;
    word-break: break-all;
}

.launch-progress-track {
    width: 100%;
    height: 12px;
    background: #090607;
    border-radius: 999px;
    overflow: hidden;
    border: 1px solid rgba(255, 0, 34, 0.42);
    margin-bottom: 0.85rem;
}

.launch-progress-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--primary-light));
    background-size: 200% 100%;
    animation: shimmerBar 1.4s linear infinite;
    transition: width 0.18s ease;
}

.launch-stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.65rem;
    margin-bottom: 0.85rem;
}

.launch-stat-box {
    background: rgba(0, 0, 0, 0.72);
    border: 1px solid rgba(255, 0, 34, 0.22);
    border-radius: 4px;
    padding: 0.55rem 0.75rem;
}

.launch-stat-label {
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--text-muted);
}

.launch-stat-value {
    font-family: 'Audiowide', cursive;
    font-size: 0.9rem;
    color: var(--gold);
    margin-top: 0.15rem;
}

.launch-error-box {
    display: none;
    margin-top: 0.7rem;
    padding: 0.7rem 0.95rem;
    border-radius: 4px;
    background: rgba(255, 0, 34, 0.14);
    border: 1px solid rgba(255, 0, 34, 0.55);
    color: #ff99a8;
    font-size: 0.96rem;
    font-weight: 600;
}

.launch-error-box.visible {
    display: block;
}

.launch-ready-actions {
    display: none;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-top: 0.85rem;
    padding-top: 0.8rem;
    border-top: 1px solid rgba(255, 0, 34, 0.22);
}

.launch-ready-actions.visible {
    display: flex;
}

.launch-url-pill {
    background: rgba(255, 0, 34, 0.12);
    border: 1px solid rgba(255, 0, 34, 0.45);
    border-radius: 4px;
    padding: 0.42rem 0.8rem;
    font-size: 0.94rem;
    font-weight: 700;
    color: var(--text);
}

/* Catalog Grid & Cards */
.status-banner {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.98rem;
    margin-bottom: 1.15rem;
    min-height: 1.4rem;
}

.games-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(225px, 1fr));
    gap: 1.1rem;
    width: 100%;
}

.games-grid.hidden {
    display: none;
}

.game-card {
    background: linear-gradient(155deg, #13080a, #050506);
    border: 1px solid rgba(255, 0, 34, 0.34);
    border-radius: 6px;
    padding: 1.1rem;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 0.85rem;
    transition: all 0.24s ease;
    position: relative;
    overflow: hidden;
}

.game-card.starred {
    border-color: rgba(212, 175, 55, 0.58);
}

.game-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
    opacity: 0.75;
}

.game-card:hover {
    transform: translateY(-4px);
    border-color: var(--primary);
    box-shadow: 0 12px 24px rgba(0, 0, 0, 0.85), var(--glow-red);
}

.card-top-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
}

.game-num {
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    color: var(--gold);
    letter-spacing: 1.4px;
}

.star-btn {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(212, 175, 55, 0.32);
    color: var(--text-muted);
    border-radius: 4px;
    padding: 0.18rem 0.52rem;
    font-family: 'Audiowide', cursive;
    font-size: 0.65rem;
    letter-spacing: 0.8px;
    cursor: pointer;
    transition: all 0.2s ease;
}

.star-btn:hover {
    color: var(--gold);
    border-color: var(--gold);
    background: rgba(212, 175, 55, 0.12);
}

.star-btn.active {
    color: #0b0708;
    background: var(--gold);
    border-color: var(--gold);
}

.game-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.96rem;
    color: var(--text);
    word-break: break-word;
}

.game-meta-line {
    font-size: 0.82rem;
    color: var(--text-muted);
    font-weight: 600;
}

.game-cache-tag {
    font-size: 0.74rem;
    color: var(--primary-light);
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
}

.card-actions {
    display: flex;
    gap: 0.45rem;
    align-items: center;
}

.play-btn {
    background: linear-gradient(135deg, var(--primary-dark), var(--primary));
    color: #ffffff;
    border: 1px solid rgba(255, 0, 34, 0.6);
    padding: 0.54rem 0.85rem;
    border-radius: 4px;
    cursor: pointer;
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    letter-spacing: 1px;
    transition: all 0.2s ease;
    flex: 1;
}

.play-btn:hover {
    background: linear-gradient(135deg, var(--primary), var(--primary-light));
    box-shadow: 0 6px 16px rgba(255, 0, 34, 0.55);
}

.purge-btn {
    background: rgba(255, 255, 255, 0.04);
    color: var(--text-muted);
    border: 1px solid rgba(255, 0, 34, 0.35);
    padding: 0.54rem 0.65rem;
    border-radius: 4px;
    cursor: pointer;
    font-family: 'Audiowide', cursive;
    font-size: 0.66rem;
    transition: all 0.2s ease;
}

.purge-btn:hover {
    color: #ffffff;
    background: rgba(255, 0, 34, 0.28);
    border-color: var(--primary);
}

/* System Stats, Library, Activity & Settings */
.stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(225px, 1fr));
    gap: 1rem;
    margin-bottom: 1.25rem;
}

.stat-card {
    background: linear-gradient(160deg, rgba(19, 8, 11, 0.95), rgba(5, 5, 7, 0.98));
    border: 1px solid rgba(255, 0, 34, 0.35);
    border-radius: 6px;
    padding: 1.1rem 1.2rem;
}

.stat-card-title {
    font-family: 'Audiowide', cursive;
    font-size: 0.74rem;
    letter-spacing: 1.5px;
    color: var(--primary-light);
    text-transform: uppercase;
    margin-bottom: 0.45rem;
}

.stat-card-big {
    font-family: 'Audiowide', cursive;
    font-size: 1.38rem;
    color: var(--gold);
    margin-bottom: 0.35rem;
}

.stat-card-sub {
    font-size: 0.94rem;
    color: var(--text-muted);
}

.meter-track {
    width: 100%;
    height: 8px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    overflow: hidden;
    margin-top: 0.55rem;
}

.meter-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--primary-dark), var(--primary), var(--gold));
    transition: width 0.35s ease;
}

.info-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 1rem;
}

.info-table th {
    text-align: left;
    font-family: 'Audiowide', cursive;
    font-size: 0.74rem;
    letter-spacing: 1.2px;
    color: var(--primary-light);
    padding: 0.65rem 0.5rem;
    border-bottom: 1px solid rgba(255, 0, 34, 0.32);
}

.info-table tr {
    border-bottom: 1px solid rgba(255, 0, 34, 0.16);
}

.info-table tr:last-child {
    border-bottom: none;
}

.info-table td {
    padding: 0.62rem 0.5rem;
}

.info-table td:first-child {
    color: var(--text-muted);
    font-weight: 700;
}

.info-table td.val-gold {
    font-family: 'Audiowide', cursive;
    font-size: 0.82rem;
    color: var(--gold);
}

.settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1rem;
    margin-bottom: 1.15rem;
}

.form-field {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
}

.form-label {
    font-family: 'Audiowide', cursive;
    font-size: 0.72rem;
    letter-spacing: 1px;
    color: var(--primary-light);
}

.checkbox-row {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
    padding-top: 0.35rem;
}

.empty-state {
    grid-column: 1 / -1;
    text-align: center;
    padding: 2.2rem 1rem;
    color: var(--text-muted);
    font-size: 1.08rem;
}

.toast {
    position: fixed;
    bottom: 22px;
    left: 50%;
    transform: translateX(-50%) translateY(18px);
    background: rgba(14, 7, 9, 0.96);
    color: var(--text);
    border: 1px solid var(--primary);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.85), var(--glow-red);
    padding: 0.65rem 1.35rem;
    border-radius: 4px;
    font-family: 'Audiowide', cursive;
    font-size: 0.78rem;
    letter-spacing: 0.8px;
    z-index: 90;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.24s ease, transform 0.24s ease;
}

.toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
}

@media (max-width: 640px) {
    .top-nav {
        padding: 0.6rem 0.85rem;
    }
    main.page-host {
        padding: 1rem 0.9rem 2rem;
    }
    .brand-title {
        font-size: 1.12rem;
    }
}
</style>
</head>
<body>
<canvas id="bg-canvas"></canvas>
<div class="bg-grid"></div>

<!-- Startup Splash Screen -->
<div id="splash">
    <div class="console-splash-shell">
        <div class="console-top-bar">
            <div class="console-led-group">
                <span class="console-led"></span>
                <span class="console-led gold"></span>
                <span class="console-led green"></span>
                <span>SYS.BOOT // V11</span>
            </div>
            <div>LOCALHOST STREAM</div>
        </div>

        <div class="splash-presenter">Ali's Productions Presents</div>
        <h1 class="splash-console-title">CODEX-RETURNS</h1>
        <div class="splash-console-sub">MULTI-PAGE GAME LAUNCHER &amp; TELEMETRY DECK</div>

        <div class="boot-diag-box">
            <div class="boot-diag-line">
                <strong>INTERACTIVE CANVAS</strong>
                <span class="ok">ONLINE</span>
            </div>
            <div class="boot-diag-line">
                <strong>GITHUB REPO BRIDGE</strong>
                <span class="ok">READY</span>
            </div>
            <div class="boot-diag-line">
                <strong>LOCAL SERVER POOL</strong>
                <span class="ok">ARMED</span>
            </div>
        </div>

        <div class="boot-gauge-wrap">
            <div class="boot-gauge-meta">
                <span class="boot-gauge-label" id="boot-gauge-label">INITIALIZING LAUNCHER...</span>
                <span class="boot-gauge-pct" id="boot-gauge-pct">0%</span>
            </div>
            <div class="boot-gauge-track">
                <div class="boot-gauge-fill" id="boot-gauge-fill"></div>
            </div>
        </div>

        <div class="splash-actions">
            <div class="splash-status-text" id="splash-status">Preparing workspace...</div>
            <button type="button" class="action-btn" id="enter-console-btn">ENTER LAUNCHER</button>
        </div>
    </div>
</div>

<!-- Top Navigation Bar -->
<nav class="top-nav" aria-label="Primary Navigation">
    <div class="top-nav-left">
        <div class="brand-block">
            <span class="brand-presenter">Ali's Productions Presents</span>
            <span class="brand-title">CODEX-RETURNS</span>
        </div>
    </div>

    <div class="top-nav-tabs" role="tablist">
        <button type="button" class="nav-tab-btn active" data-page="home">HOME</button>
        <button type="button" class="nav-tab-btn" data-page="games">GAMES</button>
        <button type="button" class="nav-tab-btn" data-page="library">LIBRARY</button>
        <button type="button" class="nav-tab-btn" data-page="activity">ACTIVITY</button>
        <button type="button" class="nav-tab-btn" data-page="stats">STATS</button>
        <button type="button" class="nav-tab-btn" data-page="settings">SETTINGS</button>
    </div>

    <div class="top-nav-telemetry">
        <div class="telem-pill" title="Current Time">
            <span class="telem-key">TIME</span>
            <span class="accent-gold" id="nav-clock">--:--:--</span>
        </div>
        <div class="telem-pill" title="Battery Status">
            <span class="telem-key">PWR</span>
            <span class="accent-green" id="nav-battery">--%</span>
        </div>
    </div>
</nav>

<div class="workspace-shell">
    <main class="page-host">
        <!-- Active Local Server Ports Bar -->
        <section class="active-ports-bar" aria-label="Active Local Game Servers">
            <div class="active-ports-title">
                <span class="active-ports-dot"></span>
                <span>ACTIVE GAME PORTS</span>
            </div>
            <div class="active-ports-list" id="active-ports-list">
                <span class="active-ports-empty">No game servers running right now.</span>
            </div>
        </section>

        <!-- HOME PAGE -->
        <section class="page-view active" id="page-home">
            <section class="quote-banner" aria-label="Featured Quote">
                <div class="quote-lines" id="quote-lines"></div>
                <div class="quote-footer-bar">
                    <div class="quote-author" id="quote-author"></div>
                    <button type="button" class="cycle-quote-btn" id="cycle-quote-btn">NEXT QUOTE</button>
                </div>
            </section>

            <div class="surface-panel">
                <div class="section-heading-row">
                    <h2 class="section-heading">FEATURED <span>GAMES</span></h2>
                    <div class="controls-group">
                        <button type="button" class="action-btn compact secondary-btn" id="home-shuffle-btn">SHUFFLE FEATURED</button>
                        <button type="button" class="action-btn compact secondary-btn" id="home-random-btn">RANDOM LAUNCH</button>
                        <button type="button" class="action-btn compact" id="home-all-games-btn">BROWSE ALL GAMES</button>
                    </div>
                </div>
                <div class="featured-grid" id="featured-grid">
                    <div class="empty-state">Loading featured games...</div>
                </div>
            </div>
        </section>

        <!-- GAMES PAGE -->
        <section class="page-view" id="page-games">
            <div class="surface-panel">
                <div class="controls-top" id="controls-top">
                    <div class="section-heading" style="margin:0;">GAME <span>CATALOG</span></div>
                    <div class="controls-group">
                        <button type="button" class="action-btn compact secondary-btn" id="random-game-btn">RANDOM GAME</button>
                        <button type="button" class="action-btn compact secondary-btn" id="refresh-btn">REFRESH LIST</button>
                        <button type="button" class="action-btn compact secondary-btn" id="stop-all-btn">STOP ALL SERVERS</button>
                    </div>
                </div>

                <div class="controls-sub" id="controls-sub">
                    <div class="search-bar">
                        <input type="text" id="search-input" placeholder="Search games by name or number (press / to focus)..." autocomplete="off" />
                        <button type="button" class="clear-search-btn" id="clear-search-btn">CLEAR</button>
                    </div>
                    <select class="sort-select" id="sort-select" aria-label="Sort games">
                        <option value="number-asc">Sort: Catalog Order</option>
                        <option value="name-asc">Sort: Name (A-Z)</option>
                        <option value="name-desc">Sort: Name (Z-A)</option>
                        <option value="cached-first">Sort: Cached First</option>
                        <option value="most-played">Sort: Most Played</option>
                    </select>
                </div>

                <!-- Focused Game Download & Launch Panel -->
                <section class="launch-panel" id="launch-panel" aria-live="polite">
                    <div class="launch-top-meta">
                        <div class="launch-game-heading" id="launch-game-heading">
                            LAUNCHING GAME <span>#1</span>
                        </div>
                        <div class="launch-page-controls">
                            <button type="button" class="force-toggle-btn" id="force-redownload-btn">FORCE RE-DOWNLOAD: OFF</button>
                            <button type="button" class="back-catalog-btn" id="back-catalog-btn">ALL GAMES</button>
                        </div>
                    </div>

                    <div class="launch-stage-line" id="launch-stage-line">Preparing game...</div>
                    <div class="launch-file-line" id="launch-file-line">Waiting for task...</div>

                    <div class="launch-progress-track">
                        <div class="launch-progress-fill" id="launch-progress-fill"></div>
                    </div>

                    <div class="launch-stats-grid">
                        <div class="launch-stat-box">
                            <div class="launch-stat-label">Progress</div>
                            <div class="launch-stat-value" id="launch-pct-val">0%</div>
                        </div>
                        <div class="launch-stat-box">
                            <div class="launch-stat-label">Files</div>
                            <div class="launch-stat-value" id="launch-files-val">0 / 0</div>
                        </div>
                        <div class="launch-stat-box">
                            <div class="launch-stat-label">Speed</div>
                            <div class="launch-stat-value" id="launch-speed-val">0.0 KB/s</div>
                        </div>
                        <div class="launch-stat-box">
                            <div class="launch-stat-label">Port</div>
                            <div class="launch-stat-value" id="launch-port-val">-</div>
                        </div>
                    </div>

                    <div class="launch-error-box" id="launch-error-box"></div>

                    <div class="launch-ready-actions" id="launch-ready-actions">
                        <div class="launch-url-pill" id="launch-url-pill">http://127.0.0.1:8001/index.html</div>
                        <button type="button" class="action-btn" id="reopen-window-btn">OPEN GAME WINDOW</button>
                        <button type="button" class="action-btn secondary-btn" id="copy-url-btn">COPY URL</button>
                    </div>
                </section>

                <!-- Dynamic Favorites Section (Shown when >= 1 game is starred) -->
                <section class="favorites-shelf" id="favorites-shelf" aria-label="Favorite Games">
                    <div class="favorites-header">
                        <div class="favorites-title">FAVORITES</div>
                        <div class="favorites-count" id="favorites-count">0 STARRED</div>
                    </div>
                    <div class="games-grid" id="favorites-grid"></div>
                </section>

                <div class="status-banner" id="status-banner">Loading games...</div>
                <div class="games-grid" id="games-grid"></div>
            </div>
        </section>

        <!-- LIBRARY PAGE (INSTALLED & CACHED) -->
        <section class="page-view" id="page-library">
            <div class="surface-panel">
                <div class="section-heading-row">
                    <h2 class="section-heading">INSTALLED &amp; <span>CACHED LIBRARY</span></h2>
                    <div class="controls-group">
                        <span class="telem-pill" id="library-summary-pill">0 CACHED | 0 B</span>
                        <button type="button" class="action-btn compact secondary-btn" id="refresh-library-btn">REFRESH CACHE</button>
                        <button type="button" class="action-btn compact secondary-btn" id="clear-all-cache-btn">CLEAR ALL CACHE</button>
                    </div>
                </div>
                <div class="games-grid" id="library-grid">
                    <div class="empty-state">No cached games yet. Launch a game to store it locally.</div>
                </div>
            </div>
        </section>

        <!-- ACTIVITY PAGE (PLAY LOG) -->
        <section class="page-view" id="page-activity">
            <div class="surface-panel">
                <div class="section-heading-row">
                    <h2 class="section-heading">PLAY <span>ACTIVITY LOG</span></h2>
                    <div class="controls-group">
                        <button type="button" class="action-btn compact secondary-btn" id="clear-activity-btn">CLEAR LOG</button>
                    </div>
                </div>
                <table class="info-table">
                    <thead>
                        <tr>
                            <th>TIMESTAMP</th>
                            <th>GAME</th>
                            <th>PORT</th>
                            <th>SOURCE</th>
                        </tr>
                    </thead>
                    <tbody id="activity-tbody">
                        <tr><td colspan="4" class="empty-state">No launch activity recorded yet.</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- SYSTEM STATS PAGE -->
        <section class="page-view" id="page-stats">
            <div class="surface-panel">
                <div class="section-heading-row">
                    <h2 class="section-heading">SYSTEM &amp; LAUNCHER <span>TELEMETRY</span></h2>
                    <button type="button" class="action-btn compact secondary-btn" id="refresh-stats-btn">REFRESH STATS</button>
                </div>

                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-card-title">CPU Load</div>
                        <div class="stat-card-big" id="stat-cpu-val">--%</div>
                        <div class="stat-card-sub" id="stat-cpu-sub">Logical Cores: --</div>
                        <div class="meter-track"><div class="meter-fill" id="stat-cpu-bar"></div></div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Memory (RAM)</div>
                        <div class="stat-card-big" id="stat-ram-val">--%</div>
                        <div class="stat-card-sub" id="stat-ram-sub">-- / --</div>
                        <div class="meter-track"><div class="meter-fill" id="stat-ram-bar"></div></div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Storage Disk</div>
                        <div class="stat-card-big" id="stat-disk-val">--%</div>
                        <div class="stat-card-sub" id="stat-disk-sub">Free: -- / --</div>
                        <div class="meter-track"><div class="meter-fill" id="stat-disk-bar"></div></div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Battery &amp; Clock</div>
                        <div class="stat-card-big" id="stat-bat-val">--</div>
                        <div class="stat-card-sub" id="stat-clock-sub">--:--:--</div>
                    </div>
                </div>

                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-card-title">Total Launches</div>
                        <div class="stat-card-big" id="stat-launches-val">0</div>
                        <div class="stat-card-sub" id="stat-favorites-sub">Favorites: 0</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Peak Transfer Rate</div>
                        <div class="stat-card-big" id="stat-peak-speed-val">0.0 KB/s</div>
                        <div class="stat-card-sub" id="stat-bytes-dl-val">Downloaded: 0 B</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Process Footprint</div>
                        <div class="stat-card-big" id="stat-proc-mem-val">--</div>
                        <div class="stat-card-sub" id="stat-proc-threads-val">Threads: --</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-card-title">Session Uptime</div>
                        <div class="stat-card-big" id="stat-uptime-val">0s</div>
                        <div class="stat-card-sub" id="stat-pid-sub">PID: --</div>
                    </div>
                </div>

                <table class="info-table">
                    <tbody>
                        <tr><td>Operating System</td><td class="val-gold" id="info-os">--</td></tr>
                        <tr><td>Architecture</td><td class="val-gold" id="info-arch">--</td></tr>
                        <tr><td>Python Runtime</td><td class="val-gold" id="info-python">--</td></tr>
                        <tr><td>Host / Local IP</td><td class="val-gold" id="info-net">--</td></tr>
                        <tr><td>Cache Footprint</td><td class="val-gold" id="info-cache">--</td></tr>
                        <tr><td>Active Game Servers</td><td class="val-gold" id="info-servers">--</td></tr>
                        <tr><td>Most Played Game</td><td class="val-gold" id="info-top-game">None yet</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- SETTINGS PAGE -->
        <section class="page-view" id="page-settings">
            <div class="surface-panel">
                <div class="section-heading-row">
                    <h2 class="section-heading">RUNTIME &amp; <span>SETTINGS</span></h2>
                    <div class="controls-group">
                        <button type="button" class="action-btn compact secondary-btn" id="reset-settings-btn">RESET DEFAULTS</button>
                        <button type="button" class="action-btn compact" id="save-settings-btn">SAVE SETTINGS</button>
                    </div>
                </div>

                <div class="settings-grid">
                    <div class="form-field">
                        <label class="form-label" for="cfg-repo-url">GITHUB REPOSITORY URL</label>
                        <input class="form-input" type="text" id="cfg-repo-url" />
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-branch">DEFAULT BRANCH</label>
                        <input class="form-input" type="text" id="cfg-branch" />
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-games-folder">GAMES FOLDER</label>
                        <input class="form-input" type="text" id="cfg-games-folder" />
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-max-workers">DOWNLOAD WORKERS (1-64)</label>
                        <input class="form-input" type="number" id="cfg-max-workers" min="1" max="64" />
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-github-token">GITHUB TOKEN (OPTIONAL)</label>
                        <input class="form-input" type="password" id="cfg-github-token" placeholder="ghp_..." />
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-bg-mode">BACKGROUND POINTER RESPONSE</label>
                        <select class="form-select" id="cfg-bg-mode">
                            <option value="repel">Repel &amp; Deflect</option>
                            <option value="attract">Swirl &amp; Orbit</option>
                            <option value="pulse">Wave &amp; Ripple</option>
                        </select>
                    </div>
                    <div class="form-field">
                        <label class="form-label" for="cfg-bg-density">BACKGROUND PARTICLE DENSITY (20-160)</label>
                        <input class="form-input" type="number" id="cfg-bg-density" min="20" max="160" />
                    </div>
                    <div class="form-field">
                        <label class="form-label">GAME WINDOW BOOT</label>
                        <label class="checkbox-row">
                            <input type="checkbox" id="cfg-boot-overlay" />
                            <span>Show CodeX-Returns boot screen in launched games</span>
                        </label>
                    </div>
                </div>
            </div>
        </section>
    </main>
</div>

<div class="toast" id="toast"></div>

<script>
// Deeply Interactive Background Canvas
const bgController = (function initInteractiveBg() {
    const canvas = document.getElementById("bg-canvas");
    if (!canvas) return { triggerPulse: () => {}, setMode: () => {}, setDensity: () => {} };
    const ctx = canvas.getContext("2d");
    let width = 0, height = 0;
    let mode = "repel";
    let targetCount = 84;
    let energyPulse = 0;

    const pointer = {
        x: -9999,
        y: -9999,
        vx: 0,
        vy: 0,
        prevX: null,
        prevY: null,
        active: false,
        down: false
    };

    const nodes = [];
    const ripples = [];
    const trail = [];

    function makeNode(x, y, burst = false) {
        const angle = Math.random() * Math.PI * 2;
        const speed = burst ? 1.8 + Math.random() * 3.4 : 0.35 + Math.random() * 0.85;
        return {
            x: x ?? Math.random() * width,
            y: y ?? Math.random() * height,
            vx: Math.cos(angle) * speed,
            vy: Math.sin(angle) * speed,
            baseR: burst ? 2.2 + Math.random() * 1.6 : 1.3 + Math.random() * 2.2,
            phase: Math.random() * Math.PI * 2,
            gold: Math.random() < 0.18,
            burst: burst
        };
    }

    function resize() {
        width = canvas.width = window.innerWidth;
        height = canvas.height = window.innerHeight;
        if (!nodes.length) {
            for (let i = 0; i < targetCount; i++) nodes.push(makeNode());
        }
    }

    function setDensity(n) {
        targetCount = Math.max(20, Math.min(160, parseInt(n, 10) || 84));
        while (nodes.length < targetCount) nodes.push(makeNode());
        while (nodes.length > targetCount) nodes.pop();
    }

    function triggerPulse(x, y, strength = 1.0) {
        energyPulse = Math.min(1.6, energyPulse + 0.65 * strength);
        const cx = (typeof x === "number" && x >= 0) ? x : width * 0.5;
        const cy = (typeof y === "number" && y >= 0) ? y : height * 0.5;
        ripples.push({
            x: cx,
            y: cy,
            radius: 8,
            maxRadius: Math.max(220, Math.min(width, height) * 0.42),
            alpha: 0.72,
            gold: Math.random() < 0.35
        });
        const burstCount = 10;
        for (let i = 0; i < burstCount; i++) {
            const ang = (Math.PI * 2 * i) / burstCount + Math.random() * 0.25;
            const spd = 1.6 + Math.random() * 2.8 * strength;
            const n = makeNode(cx, cy, true);
            n.vx = Math.cos(ang) * spd;
            n.vy = Math.sin(ang) * spd;
            nodes.push(n);
            if (nodes.length > targetCount + 45) {
                const idx = nodes.findIndex(item => item.burst);
                if (idx !== -1) nodes.splice(idx, 1);
            }
        }
    }

    window.addEventListener("resize", resize);

    window.addEventListener("mousemove", (e) => {
        if (pointer.prevX !== null && pointer.prevY !== null) {
            pointer.vx = e.clientX - pointer.prevX;
            pointer.vy = e.clientY - pointer.prevY;
        }
        pointer.prevX = e.clientX;
        pointer.prevY = e.clientY;
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        pointer.active = true;
        trail.push({ x: e.clientX, y: e.clientY, alpha: 0.55 });
        if (trail.length > 22) trail.shift();
    });

    window.addEventListener("mousedown", (e) => {
        pointer.down = true;
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        pointer.active = true;
        triggerPulse(e.clientX, e.clientY, 1.15);
    });

    window.addEventListener("mouseup", () => {
        pointer.down = false;
    });

    window.addEventListener("mouseleave", () => {
        pointer.active = false;
        pointer.down = false;
    });

    window.addEventListener("keydown", (e) => {
        if (e.key === "Shift" || e.key === "Alt") {
            triggerPulse(pointer.active ? pointer.x : width * 0.5, pointer.active ? pointer.y : height * 0.5, 0.7);
        }
    });

    function tick(now) {
        const t = (now || 0) * 0.001;
        ctx.clearRect(0, 0, width, height);

        energyPulse *= 0.96;
        pointer.vx *= 0.85;
        pointer.vy *= 0.85;

        // Subtle ambient reactive glow around pointer
        if (pointer.active) {
            const grad = ctx.createRadialGradient(pointer.x, pointer.y, 4, pointer.x, pointer.y, 190);
            grad.addColorStop(0, "rgba(255, 0, 34, 0.14)");
            grad.addColorStop(0.55, "rgba(212, 175, 55, 0.04)");
            grad.addColorStop(1, "rgba(255, 0, 34, 0)");
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(pointer.x, pointer.y, 190, 0, Math.PI * 2);
            ctx.fill();
        }

        // Pointer slipstream trail
        for (let i = 0; i < trail.length; i++) {
            const tr = trail[i];
            tr.alpha *= 0.91;
            if (tr.alpha > 0.02) {
                ctx.beginPath();
                ctx.arc(tr.x, tr.y, 2.2 * tr.alpha + 0.6, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(255, 46, 76, ${tr.alpha * 0.45})`;
                ctx.fill();
            }
        }

        // Shockwave ripples
        for (let i = ripples.length - 1; i >= 0; i--) {
            const rp = ripples[i];
            rp.radius += 5.2;
            rp.alpha *= 0.955;
            if (rp.alpha < 0.02 || rp.radius > rp.maxRadius) {
                ripples.splice(i, 1);
                continue;
            }
            ctx.beginPath();
            ctx.arc(rp.x, rp.y, rp.radius, 0, Math.PI * 2);
            ctx.strokeStyle = rp.gold
                ? `rgba(212, 175, 55, ${rp.alpha * 0.55})`
                : `rgba(255, 0, 34, ${rp.alpha * 0.65})`;
            ctx.lineWidth = 1.6;
            ctx.stroke();
        }

        // Update and draw nodes
        for (let i = 0; i < nodes.length; i++) {
            const n = nodes[i];
            n.phase += 0.03;

            if (pointer.active) {
                const dx = pointer.x - n.x;
                const dy = pointer.y - n.y;
                const dist = Math.hypot(dx, dy);
                const radius = pointer.down ? 240 : 175;
                if (dist < radius && dist > 1) {
                    const pull = (1 - dist / radius);
                    if (mode === "attract") {
                        n.vx += (dx / dist) * pull * 0.14 - (dy / dist) * pull * 0.06;
                        n.vy += (dy / dist) * pull * 0.14 + (dx / dist) * pull * 0.06;
                    } else if (mode === "pulse") {
                        const wave = Math.sin(dist * 0.04 - t * 6.0) * pull * 0.16;
                        n.vx += (dx / dist) * wave;
                        n.vy += (dy / dist) * wave;
                    } else {
                        const force = pointer.down ? 0.28 : 0.16;
                        n.vx -= (dx / dist) * pull * force;
                        n.vy -= (dy / dist) * pull * force;
                    }
                    n.vx += pointer.vx * pull * 0.012;
                    n.vy += pointer.vy * pull * 0.012;
                }
            }

            // Ripple push
            for (let rIdx = 0; rIdx < ripples.length; rIdx++) {
                const rp = ripples[rIdx];
                const rdx = n.x - rp.x;
                const rdy = n.y - rp.y;
                const rdist = Math.hypot(rdx, rdy);
                if (Math.abs(rdist - rp.radius) < 28 && rdist > 1) {
                    n.vx += (rdx / rdist) * 0.22;
                    n.vy += (rdy / rdist) * 0.22;
                }
            }

            n.vx *= n.burst ? 0.96 : 0.986;
            n.vy *= n.burst ? 0.96 : 0.986;
            n.x += n.vx;
            n.y += n.vy;

            if (n.x < 0) n.x = width;
            if (n.x > width) n.x = 0;
            if (n.y < 0) n.y = height;
            if (n.y > height) n.y = 0;

            const rad = n.baseR + Math.sin(n.phase) * 0.45 + energyPulse * 0.45;
            ctx.beginPath();
            ctx.arc(n.x, n.y, Math.max(0.8, rad), 0, Math.PI * 2);
            ctx.fillStyle = n.gold
                ? "rgba(212, 175, 55, 0.78)"
                : `rgba(255, 0, 34, ${Math.min(0.9, 0.62 + energyPulse * 0.15)})`;
            ctx.fill();

            // Connect nearby nodes
            const linkDist = 122 + energyPulse * 14;
            for (let j = i + 1; j < nodes.length; j++) {
                const q = nodes[j];
                const d = Math.hypot(n.x - q.x, n.y - q.y);
                if (d < linkDist) {
                    const alpha = (1 - d / linkDist) * (0.16 + energyPulse * 0.08);
                    ctx.beginPath();
                    ctx.moveTo(n.x, n.y);
                    ctx.lineTo(q.x, q.y);
                    ctx.strokeStyle = (n.gold || q.gold)
                        ? `rgba(212, 175, 55, ${alpha * 0.85})`
                        : `rgba(255, 0, 34, ${alpha})`;
                    ctx.lineWidth = 1;
                    ctx.stroke();
                }
            }
        }

        requestAnimationFrame(tick);
    }

    resize();
    requestAnimationFrame(tick);

    return {
        triggerPulse,
        setMode(newMode) {
            if (["repel", "attract", "pulse"].includes(newMode)) {
                mode = newMode;
            }
        },
        setDensity
    };
})();

// Launcher Application Logic
let allGames = [];
let featuredGames = [];
let favoriteNumbers = new Set();
let playCounts = {};
let activityLog = [];
let launcherMetrics = { total_launches: 0, peak_speed_bps: 0, total_downloaded_bytes: 0 };
let currentConfig = {};

let activeGameNumber = null;
let isLaunching = false;
let forceRedownload = false;
let catalogViewMode = "grid";
let lastLaunchedInfo = null;
let currentPage = "home";

const splashEl = document.getElementById("splash");
const bootGaugeFill = document.getElementById("boot-gauge-fill");
const bootGaugePct = document.getElementById("boot-gauge-pct");
const bootGaugeLabel = document.getElementById("boot-gauge-label");
const splashStatusEl = document.getElementById("splash-status");
const enterConsoleBtn = document.getElementById("enter-console-btn");

const quoteLinesEl = document.getElementById("quote-lines");
const quoteAuthorEl = document.getElementById("quote-author");
const cycleQuoteBtn = document.getElementById("cycle-quote-btn");
const featuredGridEl = document.getElementById("featured-grid");

const statusBanner = document.getElementById("status-banner");
const gamesGrid = document.getElementById("games-grid");
const favoritesShelf = document.getElementById("favorites-shelf");
const favoritesGrid = document.getElementById("favorites-grid");
const favoritesCountEl = document.getElementById("favorites-count");
const searchInput = document.getElementById("search-input");
const clearSearchBtn = document.getElementById("clear-search-btn");
const sortSelect = document.getElementById("sort-select");
const refreshBtn = document.getElementById("refresh-btn");
const stopAllBtn = document.getElementById("stop-all-btn");
const randomGameBtn = document.getElementById("random-game-btn");
const controlsTop = document.getElementById("controls-top");
const controlsSub = document.getElementById("controls-sub");

const launchPanel = document.getElementById("launch-panel");
const launchGameHeading = document.getElementById("launch-game-heading");
const forceRedownloadBtn = document.getElementById("force-redownload-btn");
const backCatalogBtn = document.getElementById("back-catalog-btn");
const launchStageLine = document.getElementById("launch-stage-line");
const launchFileLine = document.getElementById("launch-file-line");
const launchProgressFill = document.getElementById("launch-progress-fill");
const launchPctVal = document.getElementById("launch-pct-val");
const launchFilesVal = document.getElementById("launch-files-val");
const launchSpeedVal = document.getElementById("launch-speed-val");
const launchPortVal = document.getElementById("launch-port-val");
const launchErrorBox = document.getElementById("launch-error-box");
const launchReadyActions = document.getElementById("launch-ready-actions");
const launchUrlPill = document.getElementById("launch-url-pill");
const reopenWindowBtn = document.getElementById("reopen-window-btn");
const copyUrlBtn = document.getElementById("copy-url-btn");
const activePortsList = document.getElementById("active-ports-list");
const toastEl = document.getElementById("toast");

let toastTimer = null;
function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2400);
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function switchPage(pageName) {
    currentPage = pageName;
    document.querySelectorAll(".page-view").forEach(el => {
        el.classList.toggle("active", el.id === "page-" + pageName);
    });
    document.querySelectorAll(".nav-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.page === pageName);
    });
    bgController.triggerPulse(window.innerWidth * 0.5, 80, 0.75);

    if (pageName === "stats") {
        refreshSystemStats();
    } else if (pageName === "library") {
        renderLibraryPage();
    } else if (pageName === "activity") {
        renderActivityPage();
    } else if (pageName === "settings") {
        populateSettingsForm();
    }
}

document.querySelectorAll("[data-page]").forEach(btn => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
});

window.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : "";
    const inInput = tag === "input" || tag === "textarea" || tag === "select";
    if (e.key === "/" && !inInput && currentPage === "games") {
        e.preventDefault();
        searchInput.focus();
    } else if (e.key === "Escape" && catalogViewMode === "launching" && !isLaunching) {
        setCatalogMode("grid");
    }
});

function setBootProgress(pct, label, statusText) {
    const clamped = Math.max(0, Math.min(100, Math.round(pct)));
    if (bootGaugeFill) bootGaugeFill.style.width = clamped + "%";
    if (bootGaugePct) bootGaugePct.textContent = clamped + "%";
    if (label && bootGaugeLabel) bootGaugeLabel.textContent = label;
    if (statusText && splashStatusEl) splashStatusEl.textContent = statusText;
}

function dismissSplash() {
    if (!splashEl || splashEl.dataset.dismissed === "1") return;
    splashEl.dataset.dismissed = "1";
    setBootProgress(100, "LAUNCHER READY", "Welcome to CodeX-Returns.");
    splashEl.classList.add("fade-out");
    setTimeout(() => {
        splashEl.style.display = "none";
    }, 480);
}

enterConsoleBtn.addEventListener("click", dismissSplash);

function renderQuote(quoteObj) {
    quoteLinesEl.innerHTML = "";
    const lines = (quoteObj && Array.isArray(quoteObj.lines) && quoteObj.lines.length)
        ? quoteObj.lines
        : ['"Stay sharp and keep building."'];
    lines.forEach(line => {
        const span = document.createElement("span");
        span.className = "quote-line";
        span.textContent = line;
        quoteLinesEl.appendChild(span);
    });
    quoteAuthorEl.textContent = (quoteObj && quoteObj.author) ? `— ${quoteObj.author}` : "— CODEX";
}

async function cycleQuote() {
    if (!window.pywebview || !window.pywebview.api) return;
    cycleQuoteBtn.disabled = true;
    try {
        const q = await window.pywebview.api.cycle_quote();
        renderQuote(q);
    } catch (e) {
        console.error(e);
    } finally {
        cycleQuoteBtn.disabled = false;
    }
}

cycleQuoteBtn.addEventListener("click", cycleQuote);

function pickFeatured(games) {
    if (!games || !games.length) return [];
    const copy = [...games];
    for (let i = copy.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy.slice(0, Math.min(6, copy.length));
}

function renderFeatured() {
    featuredGridEl.innerHTML = "";
    if (!featuredGames.length) {
        featuredGridEl.innerHTML = '<div class="empty-state">No games available yet.</div>';
        return;
    }
    featuredGames.forEach(g => {
        const plays = playCounts[String(g.number)] || 0;
        const card = document.createElement("div");
        card.className = "featured-card";
        card.innerHTML = `
            <div>
                <div class="featured-num">FEATURED #${g.number}</div>
                <div class="featured-title" style="margin-top:0.25rem;">${escapeHtml(g.name)}</div>
                <div class="featured-meta" style="margin-top:0.25rem;">${g.cached ? "Cached locally" : "Ready to stream"}${plays ? ` | ${plays} plays` : ""}</div>
            </div>
            <button type="button" class="play-btn" data-num="${g.number}">LAUNCH #${g.number}</button>
        `;
        card.querySelector(".play-btn").addEventListener("click", () => {
            switchPage("games");
            launchGame(g.number);
        });
        featuredGridEl.appendChild(card);
    });
}

function launchRandomGame() {
    if (!allGames.length) {
        showToast("No games loaded yet");
        return;
    }
    const chosen = allGames[Math.floor(Math.random() * allGames.length)];
    switchPage("games");
    launchGame(chosen.number);
}

document.getElementById("home-shuffle-btn").addEventListener("click", () => {
    featuredGames = pickFeatured(allGames);
    renderFeatured();
});
document.getElementById("home-random-btn").addEventListener("click", launchRandomGame);
document.getElementById("home-all-games-btn").addEventListener("click", () => switchPage("games"));
randomGameBtn.addEventListener("click", launchRandomGame);

function setCatalogMode(mode) {
    catalogViewMode = mode;
    if (mode === "launching") {
        controlsTop.style.display = "none";
        controlsSub.classList.add("hidden");
        statusBanner.style.display = "none";
        gamesGrid.classList.add("hidden");
        favoritesShelf.classList.remove("visible");
        launchPanel.classList.add("active");
    } else {
        controlsTop.style.display = "flex";
        controlsSub.classList.remove("hidden");
        statusBanner.style.display = "block";
        gamesGrid.classList.remove("hidden");
        launchPanel.classList.remove("active");
        renderFavoritesShelf();
    }
}

function updateForceButtonUI() {
    forceRedownloadBtn.textContent = `FORCE RE-DOWNLOAD: ${forceRedownload ? "ON" : "OFF"}`;
    forceRedownloadBtn.classList.toggle("active", forceRedownload);
}

forceRedownloadBtn.addEventListener("click", () => {
    forceRedownload = !forceRedownload;
    updateForceButtonUI();
    showToast(forceRedownload ? "Force re-download enabled" : "Force re-download disabled");
});

backCatalogBtn.addEventListener("click", () => {
    if (isLaunching) {
        showToast("Download in progress...");
        return;
    }
    setCatalogMode("grid");
});

reopenWindowBtn.addEventListener("click", async () => {
    if (!lastLaunchedInfo || !window.pywebview || !window.pywebview.api) return;
    try {
        await window.pywebview.api.open_game_window(
            lastLaunchedInfo.url,
            lastLaunchedInfo.name,
            lastLaunchedInfo.number
        );
    } catch (e) {
        showToast("Could not open game window");
    }
});

copyUrlBtn.addEventListener("click", async () => {
    if (!lastLaunchedInfo || !window.pywebview || !window.pywebview.api) return;
    const ok = await window.pywebview.api.copy_text(lastLaunchedInfo.url);
    showToast(ok ? "Copied game URL" : "Copy failed");
});

function renderActivePorts(servers) {
    document.getElementById("info-servers").textContent = String((servers && servers.length) || 0);
    activePortsList.innerHTML = "";
    if (!servers || !servers.length) {
        activePortsList.innerHTML = '<span class="active-ports-empty">No game servers running right now.</span>';
        return;
    }
    servers.forEach(srv => {
        const group = document.createElement("div");
        group.className = "port-pill-group";

        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "port-pill-copy";
        copyBtn.title = `Click to copy ${srv.url}`;
        copyBtn.innerHTML = `<strong>:${srv.port}</strong> <span>#${srv.number} ${escapeHtml(srv.name)}</span>`;
        copyBtn.addEventListener("click", async () => {
            if (!window.pywebview || !window.pywebview.api) return;
            const ok = await window.pywebview.api.copy_text(srv.url);
            showToast(ok ? `Copied ${srv.url}` : "Copy failed");
        });

        const stopBtn = document.createElement("button");
        stopBtn.type = "button";
        stopBtn.className = "port-pill-stop";
        stopBtn.title = `Stop server on port ${srv.port}`;
        stopBtn.textContent = "STOP";
        stopBtn.addEventListener("click", async () => {
            if (!window.pywebview || !window.pywebview.api) return;
            const res = await window.pywebview.api.stop_server(srv.port);
            if (res && res.ok) {
                renderActivePorts(res.servers || []);
                showToast(`Stopped port :${srv.port}`);
            }
        });

        group.appendChild(copyBtn);
        group.appendChild(stopBtn);
        activePortsList.appendChild(group);
    });
}

async function toggleFavorite(gameNumber) {
    const num = Number(gameNumber);
    if (!window.pywebview || !window.pywebview.api) return;
    const res = await window.pywebview.api.toggle_favorite(num);
    if (res && Array.isArray(res.favorites)) {
        favoriteNumbers = new Set(res.favorites.map(Number));
        renderFavoritesShelf();
        applySearchFilter();
        showToast(favoriteNumbers.has(num) ? `Added #${num} to Favorites` : `Removed #${num} from Favorites`);
    }
}

function buildGameCard(game) {
    const isFav = favoriteNumbers.has(Number(game.number));
    const plays = playCounts[String(game.number)] || 0;
    const card = document.createElement("div");
    card.className = "game-card" + (isFav ? " starred" : "");
    card.innerHTML = `
        <div>
            <div class="card-top-row">
                <span class="game-num">GAME #${game.number}</span>
                <button type="button" class="star-btn ${isFav ? "active" : ""}" title="${isFav ? "Remove from Favorites" : "Add to Favorites"}">${isFav ? "STARRED" : "STAR"}</button>
            </div>
            <div class="game-title" style="margin-top:0.35rem;">${escapeHtml(game.name)}</div>
            <div class="game-meta-line" style="margin-top:0.25rem;">${plays} play${plays === 1 ? "" : "s"}</div>
            ${game.cached ? '<div class="game-cache-tag" style="margin-top:0.3rem;">CACHED</div>' : ""}
        </div>
        <div class="card-actions">
            <button type="button" class="play-btn" data-num="${game.number}">PLAY</button>
        </div>
    `;
    card.querySelector(".star-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        toggleFavorite(game.number);
    });
    card.querySelector(".play-btn").addEventListener("click", () => launchGame(game.number));
    return card;
}

function renderFavoritesShelf() {
    if (catalogViewMode === "launching") {
        favoritesShelf.classList.remove("visible");
        return;
    }
    const starredGames = allGames.filter(g => favoriteNumbers.has(Number(g.number)));
    favoritesCountEl.textContent = `${starredGames.length} STARRED`;
    if (!starredGames.length) {
        favoritesShelf.classList.remove("visible");
        favoritesGrid.innerHTML = "";
        return;
    }
    favoritesShelf.classList.add("visible");
    favoritesGrid.innerHTML = "";
    starredGames.forEach(g => favoritesGrid.appendChild(buildGameCard(g)));
}

function getSortedGames(list) {
    const mode = sortSelect.value;
    const arr = [...list];
    if (mode === "name-asc") {
        arr.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    } else if (mode === "name-desc") {
        arr.sort((a, b) => b.name.localeCompare(a.name, undefined, { sensitivity: "base" }));
    } else if (mode === "cached-first") {
        arr.sort((a, b) => (b.cached ? 1 : 0) - (a.cached ? 1 : 0) || a.number - b.number);
    } else if (mode === "most-played") {
        arr.sort((a, b) => (playCounts[String(b.number)] || 0) - (playCounts[String(a.number)] || 0) || a.number - b.number);
    } else {
        arr.sort((a, b) => a.number - b.number);
    }
    return arr;
}

function renderGames(list) {
    gamesGrid.innerHTML = "";
    renderFavoritesShelf();
    if (!list.length) {
        gamesGrid.innerHTML = '<div class="empty-state">No matching games found.</div>';
        return;
    }
    list.forEach(game => gamesGrid.appendChild(buildGameCard(game)));
}

function applySearchFilter() {
    const q = searchInput.value.trim().toLowerCase();
    clearSearchBtn.style.display = q ? "inline-block" : "none";
    const sorted = getSortedGames(allGames);
    if (!q) {
        renderGames(sorted);
        statusBanner.textContent = allGames.length
            ? `${allGames.length} games available — select any game to launch.`
            : "No games found in repository.";
        return;
    }
    const cleanNum = q.startsWith("#") ? q.slice(1) : q;
    const filtered = sorted.filter(g =>
        g.name.toLowerCase().includes(q) ||
        String(g.number) === cleanNum ||
        String(g.number).includes(cleanNum)
    );
    renderGames(filtered);
    statusBanner.textContent = `Showing ${filtered.length} of ${allGames.length} games.`;
}

searchInput.addEventListener("input", applySearchFilter);
sortSelect.addEventListener("change", applySearchFilter);
clearSearchBtn.addEventListener("click", () => {
    searchInput.value = "";
    applySearchFilter();
    searchInput.focus();
});

function setControlsBusy(busy) {
    isLaunching = busy;
    refreshBtn.disabled = busy;
    stopAllBtn.disabled = busy;
    randomGameBtn.disabled = busy;
    backCatalogBtn.disabled = busy;
    forceRedownloadBtn.disabled = busy;
    document.querySelectorAll(".play-btn").forEach(b => (b.disabled = busy));
}

function updateLaunchProgress(payload) {
    if (!payload) return;
    if (typeof payload.game_number === "number") {
        activeGameNumber = payload.game_number;
        const namePart = payload.game_name ? ` — ${escapeHtml(payload.game_name)}` : "";
        launchGameHeading.innerHTML = `LAUNCHING GAME <span>#${payload.game_number}</span>${namePart}`;
    }
    if (payload.stage) launchStageLine.textContent = payload.stage;
    if (payload.current_file !== undefined) {
        launchFileLine.textContent = payload.current_file ? `File: ${payload.current_file}` : "";
    }
    if (typeof payload.percent === "number") {
        const pct = Math.max(0, Math.min(100, payload.percent));
        launchProgressFill.style.width = pct + "%";
        launchPctVal.textContent = `${pct.toFixed(1)}%`;
    }
    if (typeof payload.completed_files === "number" && typeof payload.total_files === "number") {
        launchFilesVal.textContent = `${payload.completed_files} / ${payload.total_files}`;
    }
    if (payload.speed_text) launchSpeedVal.textContent = payload.speed_text;
    if (payload.port) launchPortVal.textContent = `:${payload.port}`;
}
window.updateLaunchProgress = updateLaunchProgress;

async function launchGame(gameNumber) {
    if (isLaunching) return;
    const num = parseInt(gameNumber, 10);
    if (!Number.isInteger(num) || num < 1) {
        showToast("Invalid game selection");
        return;
    }
    if (!window.pywebview || !window.pywebview.api) return;

    activeGameNumber = num;
    lastLaunchedInfo = null;
    setCatalogMode("launching");
    launchErrorBox.classList.remove("visible");
    launchErrorBox.textContent = "";
    launchReadyActions.classList.remove("visible");
    launchProgressFill.style.width = "4%";
    launchPctVal.textContent = "0%";
    launchFilesVal.textContent = "0 / 0";
    launchSpeedVal.textContent = "0.0 KB/s";
    launchPortVal.textContent = "-";
    launchStageLine.textContent = `Preparing Game #${num}...`;
    launchFileLine.textContent = "Connecting to repository...";
    setControlsBusy(true);

    try {
        const res = await window.pywebview.api.launch_game_by_number(num, forceRedownload);
        if (res && Array.isArray(res.servers)) {
            renderActivePorts(res.servers);
        }
        if (res && Array.isArray(res.activity)) {
            activityLog = res.activity;
        }
        if (res && res.play_counts) {
            playCounts = res.play_counts;
        }
        if (res && res.metrics) {
            launcherMetrics = res.metrics;
        }
        if (res && res.ok) {
            lastLaunchedInfo = res;
            launchProgressFill.style.width = "100%";
            launchPctVal.textContent = "100%";
            launchPortVal.textContent = `:${res.port}`;
            launchStageLine.textContent = `Running "${res.name}" on http://127.0.0.1:${res.port}/index.html`;
            launchFileLine.textContent = "Game window opened.";
            launchUrlPill.textContent = res.url;
            launchReadyActions.classList.add("visible");
            const idx = allGames.findIndex(g => g.number === res.number);
            if (idx !== -1) allGames[idx].cached = true;
            renderFeatured();
            applySearchFilter();
            showToast(`Launched #${res.number} ${res.name}`);
        } else {
            const errMsg = (res && res.error) ? res.error : "Failed to launch game.";
            launchStageLine.textContent = "Launch failed";
            launchErrorBox.textContent = errMsg;
            launchErrorBox.classList.add("visible");
        }
    } catch (err) {
        launchStageLine.textContent = "Launch error";
        launchErrorBox.textContent = String(err);
        launchErrorBox.classList.add("visible");
    } finally {
        setControlsBusy(false);
    }
}

async function loadGamesList(forceRefresh = false) {
    if (!window.pywebview || !window.pywebview.api) return;
    setControlsBusy(true);
    statusBanner.textContent = forceRefresh
        ? "Syncing game list from GitHub..."
        : "Loading game list...";
    try {
        const data = await window.pywebview.api.get_games_list(forceRefresh);
        if (data && Array.isArray(data.servers)) {
            renderActivePorts(data.servers);
        }
        if (data && Array.isArray(data.favorites)) {
            favoriteNumbers = new Set(data.favorites.map(Number));
        }
        if (data && data.play_counts) {
            playCounts = data.play_counts;
        }
        if (data && Array.isArray(data.activity)) {
            activityLog = data.activity;
        }
        if (data && data.metrics) {
            launcherMetrics = data.metrics;
        }
        if (data && data.ok) {
            allGames = data.games || [];
            featuredGames = pickFeatured(allGames);
            renderFeatured();
            applySearchFilter();
            if (currentPage === "library") renderLibraryPage();
        } else {
            statusBanner.textContent = (data && data.error) ? data.error : "Unable to load games.";
            renderGames([]);
        }
    } catch (err) {
        statusBanner.textContent = `Error: ${err}`;
        renderGames([]);
    } finally {
        setControlsBusy(false);
    }
}

refreshBtn.addEventListener("click", () => loadGamesList(true));

async function stopAllServers() {
    if (!window.pywebview || !window.pywebview.api) return;
    const res = await window.pywebview.api.stop_all_servers();
    if (res && res.ok) {
        renderActivePorts(res.servers || []);
        showToast("Stopped all game servers");
    }
}

stopAllBtn.addEventListener("click", stopAllServers);

async function renderLibraryPage() {
    const grid = document.getElementById("library-grid");
    const pill = document.getElementById("library-summary-pill");
    if (!window.pywebview || !window.pywebview.api) return;
    try {
        const res = await window.pywebview.api.get_library_details();
        const items = (res && Array.isArray(res.items)) ? res.items : [];
        pill.textContent = `${items.length} CACHED | ${res.total_size_label || "0 B"}`;
        grid.innerHTML = "";
        if (!items.length) {
            grid.innerHTML = '<div class="empty-state">No cached games found on disk.</div>';
            return;
        }
        items.forEach(item => {
            const card = document.createElement("div");
            card.className = "game-card";
            card.innerHTML = `
                <div>
                    <div class="card-top-row">
                        <span class="game-num">GAME #${item.number}</span>
                        <span class="game-cache-tag">${escapeHtml(item.size_label)}</span>
                    </div>
                    <div class="game-title" style="margin-top:0.35rem;">${escapeHtml(item.name)}</div>
                    <div class="game-meta-line" style="margin-top:0.25rem;">${item.file_count} cached file(s)</div>
                </div>
                <div class="card-actions">
                    <button type="button" class="play-btn">PLAY</button>
                    <button type="button" class="purge-btn">REMOVE</button>
                </div>
            `;
            card.querySelector(".play-btn").addEventListener("click", () => {
                switchPage("games");
                launchGame(item.number);
            });
            card.querySelector(".purge-btn").addEventListener("click", async () => {
                const del = await window.pywebview.api.delete_game_cache(item.name);
                if (del && del.ok) {
                    showToast(`Removed cache for ${item.name}`);
                    await loadGamesList(false);
                    renderLibraryPage();
                }
            });
            grid.appendChild(card);
        });
    } catch (e) {
        console.error(e);
    }
}

document.getElementById("refresh-library-btn").addEventListener("click", renderLibraryPage);
document.getElementById("clear-all-cache-btn").addEventListener("click", async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    const res = await window.pywebview.api.clear_all_game_cache();
    if (res && res.ok) {
        showToast("Cleared all cached games");
        await loadGamesList(false);
        renderLibraryPage();
    }
});

function renderActivityPage() {
    const tbody = document.getElementById("activity-tbody");
    tbody.innerHTML = "";
    if (!activityLog || !activityLog.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No launch activity recorded yet.</td></tr>';
        return;
    }
    activityLog.forEach(entry => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td>${escapeHtml(entry.timestamp || "--")}</td>
            <td class="val-gold">#${entry.number} — ${escapeHtml(entry.name || "")}</td>
            <td>:${escapeHtml(String(entry.port || "-"))}</td>
            <td>${escapeHtml(entry.source || "Stream")}</td>
        `;
        tbody.appendChild(tr);
    });
}

document.getElementById("clear-activity-btn").addEventListener("click", async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    const res = await window.pywebview.api.clear_activity();
    if (res && res.ok) {
        activityLog = [];
        renderActivityPage();
        showToast("Cleared play activity log");
    }
});

async function refreshSystemStats() {
    if (!window.pywebview || !window.pywebview.api) return;
    try {
        const s = await window.pywebview.api.get_system_telemetry();
        if (!s) return;

        document.getElementById("nav-clock").textContent = s.time || "--:--:--";
        document.getElementById("stat-clock-sub").textContent = `${s.date || ""} | ${s.time || ""}`;

        const batLabel = (s.battery && s.battery.label) ? s.battery.label : "N/A";
        document.getElementById("nav-battery").textContent = batLabel;
        document.getElementById("stat-bat-val").textContent = batLabel;

        if (typeof s.cpu_percent === "number") {
            document.getElementById("stat-cpu-val").textContent = `${s.cpu_percent}%`;
            document.getElementById("stat-cpu-bar").style.width = `${Math.min(100, s.cpu_percent)}%`;
        } else {
            document.getElementById("stat-cpu-val").textContent = "N/A";
            document.getElementById("stat-cpu-bar").style.width = "0%";
        }
        document.getElementById("stat-cpu-sub").textContent = `Logical Cores: ${s.cpu_cores}`;

        if (typeof s.ram_percent === "number") {
            document.getElementById("stat-ram-val").textContent = `${s.ram_percent}%`;
            document.getElementById("stat-ram-bar").style.width = `${Math.min(100, s.ram_percent)}%`;
        } else {
            document.getElementById("stat-ram-val").textContent = "N/A";
            document.getElementById("stat-ram-bar").style.width = "0%";
        }
        document.getElementById("stat-ram-sub").textContent = `${s.ram_used} / ${s.ram_total}`;

        if (typeof s.disk_percent === "number") {
            document.getElementById("stat-disk-val").textContent = `${s.disk_percent}%`;
            document.getElementById("stat-disk-bar").style.width = `${Math.min(100, s.disk_percent)}%`;
        } else {
            document.getElementById("stat-disk-val").textContent = "N/A";
            document.getElementById("stat-disk-bar").style.width = "0%";
        }
        document.getElementById("stat-disk-sub").textContent = `Free: ${s.disk_free} / ${s.disk_total}`;

        document.getElementById("stat-launches-val").textContent = String(launcherMetrics.total_launches || 0);
        document.getElementById("stat-favorites-sub").textContent = `Favorites: ${favoriteNumbers.size}`;
        document.getElementById("stat-peak-speed-val").textContent = launcherMetrics.peak_speed_label || "0.0 KB/s";
        document.getElementById("stat-bytes-dl-val").textContent = `Downloaded: ${launcherMetrics.total_downloaded_label || "0 B"}`;
        document.getElementById("stat-proc-mem-val").textContent = s.proc_mem || "N/A";
        document.getElementById("stat-proc-threads-val").textContent = `Threads: ${s.proc_threads || "--"}`;
        document.getElementById("stat-uptime-val").textContent = s.uptime || "0s";
        document.getElementById("stat-pid-sub").textContent = `PID: ${s.pid || "--"}`;

        document.getElementById("info-os").textContent = s.os || "--";
        document.getElementById("info-arch").textContent = s.arch || "--";
        document.getElementById("info-python").textContent = s.python || "--";
        document.getElementById("info-net").textContent = `${s.hostname} (${s.local_ip})`;
        document.getElementById("info-cache").textContent = `${s.cache_size} (${s.cached_games} cached)`;
        document.getElementById("info-servers").textContent = String(s.active_servers);

        let topNum = null, topCount = 0;
        Object.entries(playCounts || {}).forEach(([k, v]) => {
            if (Number(v) > topCount) {
                topCount = Number(v);
                topNum = Number(k);
            }
        });
        const topGame = topNum ? allGames.find(g => g.number === topNum) : null;
        document.getElementById("info-top-game").textContent = topGame
            ? `#${topGame.number} ${topGame.name} (${topCount} plays)`
            : "None yet";
    } catch (e) {
        console.error(e);
    }
}

document.getElementById("refresh-stats-btn").addEventListener("click", refreshSystemStats);

function populateSettingsForm() {
    document.getElementById("cfg-repo-url").value = currentConfig.repo_url || "";
    document.getElementById("cfg-branch").value = currentConfig.branch || "main";
    document.getElementById("cfg-games-folder").value = currentConfig.games_folder || "files";
    document.getElementById("cfg-max-workers").value = currentConfig.max_workers || 24;
    document.getElementById("cfg-github-token").value = currentConfig.github_token || "";
    document.getElementById("cfg-bg-mode").value = currentConfig.bg_interaction_mode || "repel";
    document.getElementById("cfg-bg-density").value = currentConfig.bg_density || 84;
    document.getElementById("cfg-boot-overlay").checked = currentConfig.show_boot_overlay !== false;
}

function applyClientConfig(cfg) {
    currentConfig = cfg || {};
    bgController.setMode(currentConfig.bg_interaction_mode || "repel");
    bgController.setDensity(currentConfig.bg_density || 84);
}

document.getElementById("save-settings-btn").addEventListener("click", async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    const payload = {
        repo_url: document.getElementById("cfg-repo-url").value.trim(),
        branch: document.getElementById("cfg-branch").value.trim() || "main",
        games_folder: document.getElementById("cfg-games-folder").value.trim() || "files",
        max_workers: parseInt(document.getElementById("cfg-max-workers").value, 10) || 24,
        github_token: document.getElementById("cfg-github-token").value.trim(),
        bg_interaction_mode: document.getElementById("cfg-bg-mode").value,
        bg_density: parseInt(document.getElementById("cfg-bg-density").value, 10) || 84,
        show_boot_overlay: document.getElementById("cfg-boot-overlay").checked
    };
    const res = await window.pywebview.api.save_settings(payload);
    if (res && res.ok) {
        applyClientConfig(res.config);
        showToast("Settings saved");
        await loadGamesList(true);
    } else {
        showToast((res && res.error) ? res.error : "Failed to save settings");
    }
});

document.getElementById("reset-settings-btn").addEventListener("click", async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    const res = await window.pywebview.api.reset_settings();
    if (res && res.ok) {
        applyClientConfig(res.config);
        populateSettingsForm();
        showToast("Settings reset to defaults");
        await loadGamesList(true);
    }
});

window.addEventListener("pywebviewready", async () => {
    updateForceButtonUI();
    setBootProgress(25, "LOADING QUOTE & TELEMETRY", "Reading launcher state...");
    try {
        const initData = await window.pywebview.api.get_initial_state();
        if (initData) {
            renderQuote(initData.quote);
            renderActivePorts(initData.servers || []);
            favoriteNumbers = new Set((initData.favorites || []).map(Number));
            playCounts = initData.play_counts || {};
            activityLog = initData.activity || [];
            launcherMetrics = initData.metrics || launcherMetrics;
            applyClientConfig(initData.config || {});
            populateSettingsForm();
        }
    } catch (e) {
        console.error(e);
    }

    await refreshSystemStats();
    setBootProgress(62, "SYNCING GAME CATALOG", "Checking cache and repository...");
    await loadGamesList(false);
    setBootProgress(100, "READY", "Launcher online.");
    setTimeout(dismissSplash, 380);

    setInterval(refreshSystemStats, 2000);
});
</script>
</body>
</html>
"""


class LauncherAPI:
    def __init__(self, token: str = ""):
        self._state_lock = threading.Lock()
        self._state = self._load_state(token)
        self._apply_config_to_runtime(self._state["config"])
        self._window = None
        self._lock = threading.Lock()
        self._servers: dict[int, dict] = {}
        self._game_windows: list = []
        self._ui_server: ThreadingHTTPServer | None = None
        self._ui_port: int | None = None
        self._started_at = time.monotonic()

    def _load_state(self, env_token: str = "") -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if env_token:
            cfg["github_token"] = env_token
        favorites: list[int] = []
        play_counts: dict[str, int] = {}
        activity: list[dict] = []
        metrics = {
            "total_launches": 0,
            "peak_speed_bps": 0.0,
            "total_downloaded_bytes": 0,
        }

        if USER_STATE_PATH.exists():
            try:
                raw = json.loads(USER_STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if isinstance(raw.get("config"), dict):
                        cfg.update(raw["config"])
                    if isinstance(raw.get("favorites"), list):
                        favorites = [int(x) for x in raw["favorites"] if str(x).isdigit()]
                    if isinstance(raw.get("play_counts"), dict):
                        play_counts = {str(k): int(v) for k, v in raw["play_counts"].items()}
                    if isinstance(raw.get("activity"), list):
                        activity = raw["activity"][-100:]
                    if isinstance(raw.get("metrics"), dict):
                        metrics.update(raw["metrics"])
            except Exception:
                pass

        return {
            "config": cfg,
            "favorites": favorites,
            "play_counts": play_counts,
            "activity": activity,
            "metrics": metrics,
        }

    def _save_state(self):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            USER_STATE_PATH.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _apply_config_to_runtime(self, cfg: dict):
        repo_url = (cfg.get("repo_url") or GITHUB_REPO_URL).strip()
        self.owner, self.repo = parse_github_repo(repo_url)
        self.branch = (cfg.get("branch") or BRANCH).strip()
        self.games_folder = (cfg.get("games_folder") or GAMES_FOLDER).strip().strip("/")
        self.max_workers = max(1, min(64, int(cfg.get("max_workers") or MAX_WORKERS)))
        token = (cfg.get("github_token") or "").strip()
        self.session = build_session(token, self.max_workers)
        GameBootHTTPHandler.boot_enabled = bool(cfg.get("show_boot_overlay", True))

    def _format_metrics(self) -> dict:
        with self._state_lock:
            m = dict(self._state["metrics"])
        return {
            "total_launches": int(m.get("total_launches", 0)),
            "peak_speed_bps": float(m.get("peak_speed_bps", 0.0)),
            "peak_speed_label": format_speed(float(m.get("peak_speed_bps", 0.0))),
            "total_downloaded_bytes": int(m.get("total_downloaded_bytes", 0)),
            "total_downloaded_label": format_size(int(m.get("total_downloaded_bytes", 0))),
        }

    def set_window(self, window):
        self._window = window

    def start_ui_server(self) -> int:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        index_html = CACHE_DIR / "launcher_ui.html"
        index_html.write_text(HTML_PAGE, encoding="utf-8")
        handler = partial(QuietUIHandler, directory=str(CACHE_DIR))
        port, _ = find_free_port(None)
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._ui_server = server
        self._ui_port = port
        return port

    def _emit_progress(self, payload: dict):
        if not self._window:
            return
        try:
            js = f"window.updateLaunchProgress && window.updateLaunchProgress({json.dumps(payload)});"
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _serialize_servers(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "port": port,
                    "number": info["number"],
                    "name": info["name"],
                    "url": info["url"],
                }
                for port, info in sorted(self._servers.items())
            ]

    def _count_cached_games(self) -> int:
        if not GAMES_CACHE_DIR.exists():
            return 0
        try:
            return sum(
                1
                for p in GAMES_CACHE_DIR.iterdir()
                if p.is_dir() and (p / "index.html").is_file()
            )
        except Exception:
            return 0

    def get_initial_state(self) -> dict:
        with self._state_lock:
            favorites = list(self._state["favorites"])
            play_counts = dict(self._state["play_counts"])
            activity = list(reversed(self._state["activity"]))
            config = dict(self._state["config"])
        return {
            "quote": pick_next_quote(),
            "servers": self._serialize_servers(),
            "favorites": favorites,
            "play_counts": play_counts,
            "activity": activity,
            "metrics": self._format_metrics(),
            "config": config,
        }

    def cycle_quote(self) -> dict:
        return pick_next_quote()

    def toggle_favorite(self, game_number: int) -> dict:
        num = int(game_number)
        with self._state_lock:
            favs = self._state["favorites"]
            if num in favs:
                favs.remove(num)
            else:
                favs.append(num)
            self._save_state()
            result_favs = list(favs)
        return {"ok": True, "favorites": result_favs}

    def get_library_details(self) -> dict:
        index_games = self._load_cached_index()
        by_name = {g.get("name"): g for g in index_games if isinstance(g, dict)}
        items = []
        total_bytes = 0

        if GAMES_CACHE_DIR.exists():
            for folder in sorted(GAMES_CACHE_DIR.iterdir(), key=lambda p: p.name.lower()):
                if not folder.is_dir():
                    continue
                size_b = get_dir_size_bytes(folder)
                total_bytes += size_b
                file_cnt = sum(1 for f in folder.rglob("*") if f.is_file())
                meta = by_name.get(folder.name, {})
                items.append(
                    {
                        "number": meta.get("number", 0),
                        "name": folder.name,
                        "size_bytes": size_b,
                        "size_label": format_size(size_b),
                        "file_count": file_cnt,
                    }
                )

        return {
            "ok": True,
            "items": items,
            "total_size_label": format_size(total_bytes),
        }

    def delete_game_cache(self, game_name: str) -> dict:
        safe_name = Path(str(game_name or "")).name
        if not safe_name:
            return {"ok": False}
        target = GAMES_CACHE_DIR / safe_name
        if target.exists() and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        return {"ok": True}

    def clear_all_game_cache(self) -> dict:
        if GAMES_CACHE_DIR.exists():
            shutil.rmtree(GAMES_CACHE_DIR, ignore_errors=True)
            GAMES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    def clear_activity(self) -> dict:
        with self._state_lock:
            self._state["activity"] = []
            self._save_state()
        return {"ok": True}

    def save_settings(self, new_cfg: dict) -> dict:
        if not isinstance(new_cfg, dict):
            return {"ok": False, "error": "Invalid settings payload."}
        try:
            merged = dict(DEFAULT_CONFIG)
            with self._state_lock:
                merged.update(self._state["config"])
            merged["repo_url"] = (new_cfg.get("repo_url") or GITHUB_REPO_URL).strip()
            merged["branch"] = (new_cfg.get("branch") or BRANCH).strip()
            merged["games_folder"] = (new_cfg.get("games_folder") or GAMES_FOLDER).strip()
            merged["max_workers"] = max(1, min(64, int(new_cfg.get("max_workers") or MAX_WORKERS)))
            merged["github_token"] = (new_cfg.get("github_token") or "").strip()
            merged["bg_interaction_mode"] = new_cfg.get("bg_interaction_mode") or "repel"
            merged["bg_density"] = max(20, min(160, int(new_cfg.get("bg_density") or 84)))
            merged["show_boot_overlay"] = bool(new_cfg.get("show_boot_overlay", True))

            self._apply_config_to_runtime(merged)
            with self._state_lock:
                self._state["config"] = merged
                self._save_state()
            return {"ok": True, "config": merged}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_settings(self) -> dict:
        default_copy = dict(DEFAULT_CONFIG)
        self._apply_config_to_runtime(default_copy)
        with self._state_lock:
            self._state["config"] = default_copy
            self._save_state()
        return {"ok": True, "config": default_copy}

    def get_system_telemetry(self) -> dict:
        with self._lock:
            active_count = len(self._servers)
        return get_system_stats(
            active_servers_count=active_count,
            cached_games_count=self._count_cached_games(),
            launcher_uptime_sec=time.monotonic() - self._started_at,
        )

    def copy_text(self, text: str) -> bool:
        return copy_to_clipboard(str(text or ""))

    def _detect_branch(self) -> str:
        primary = self.branch
        fallback = "master" if primary != "master" else "main"
        for candidate in (primary, fallback):
            url = (
                f"https://api.github.com/repos/{quote(self.owner)}/{quote(self.repo)}"
                f"/git/trees/{quote(candidate)}?recursive=1"
            )
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200:
                self.branch = candidate
                return candidate
        raise RuntimeError(
            f"Could not access repo tree for {self.owner}/{self.repo} (checked {primary}/{fallback})."
        )

    def _fetch_repo_game_files(self) -> dict[str, list[str]]:
        branch = self._detect_branch()
        url = (
            f"https://api.github.com/repos/{quote(self.owner)}/{quote(self.repo)}"
            f"/git/trees/{quote(branch)}?recursive=1"
        )
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 403:
            raise RuntimeError("GitHub API rate limit reached. Try again later or add a token in Settings.")
        resp.raise_for_status()
        data = resp.json()
        if data.get("truncated"):
            raise RuntimeError("Repository tree exceeded GitHub API limit.")

        prefix = self.games_folder + "/"
        grouped: dict[str, list[str]] = {}
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix) :]
            if "/" not in rel:
                continue
            game_name, sub_path = rel.split("/", 1)
            if not game_name or not sub_path:
                continue
            grouped.setdefault(game_name, []).append(sub_path)

        for name in grouped:
            grouped[name].sort()
        return grouped

    def _load_cached_index(self) -> list[dict]:
        if INDEX_CACHE_PATH.exists():
            try:
                raw = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return raw
            except Exception:
                pass
        return []

    def _save_cached_index(self, games: list[dict]):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_CACHE_PATH.write_text(json.dumps(games, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_games_list(self, force_refresh: bool = False) -> dict:
        with self._state_lock:
            favorites = list(self._state["favorites"])
            play_counts = dict(self._state["play_counts"])
            activity = list(reversed(self._state["activity"]))

        cached_list = self._load_cached_index()
        if cached_list and not force_refresh:
            for g in cached_list:
                g["cached"] = (GAMES_CACHE_DIR / g["name"] / "index.html").is_file()
            return {
                "ok": True,
                "games": cached_list,
                "servers": self._serialize_servers(),
                "favorites": favorites,
                "play_counts": play_counts,
                "activity": activity,
                "metrics": self._format_metrics(),
            }

        try:
            grouped = self._fetch_repo_game_files()
            names = sorted(grouped.keys(), key=lambda s: s.lower())
            games = []
            for idx, name in enumerate(names, start=1):
                games.append(
                    {
                        "number": idx,
                        "name": name,
                        "files": grouped[name],
                        "cached": (GAMES_CACHE_DIR / name / "index.html").is_file(),
                    }
                )
            self._save_cached_index(games)
            return {
                "ok": True,
                "games": games,
                "servers": self._serialize_servers(),
                "favorites": favorites,
                "play_counts": play_counts,
                "activity": activity,
                "metrics": self._format_metrics(),
            }
        except Exception as e:
            if cached_list:
                for g in cached_list:
                    g["cached"] = (GAMES_CACHE_DIR / g["name"] / "index.html").is_file()
                return {
                    "ok": True,
                    "games": cached_list,
                    "servers": self._serialize_servers(),
                    "favorites": favorites,
                    "play_counts": play_counts,
                    "activity": activity,
                    "metrics": self._format_metrics(),
                    "warning": str(e),
                }
            return {
                "ok": False,
                "error": str(e),
                "servers": self._serialize_servers(),
                "favorites": favorites,
                "play_counts": play_counts,
                "activity": activity,
                "metrics": self._format_metrics(),
            }

    def _download_single_file(self, game_name: str, rel_file: str, dest_dir: Path) -> int:
        encoded_parts = "/".join(quote(part) for part in rel_file.split("/"))
        raw_url = (
            f"https://raw.githubusercontent.com/{quote(self.owner)}/{quote(self.repo)}"
            f"/{quote(self.branch)}/{quote(self.games_folder)}/{quote(game_name)}/{encoded_parts}"
        )
        resp = self.session.get(raw_url, timeout=60)
        resp.raise_for_status()
        content = resp.content

        lfs_ptr = _parse_lfs_pointer(content)
        if lfs_ptr is not None:
            oid, size = lfs_ptr
            lfs_url = _resolve_lfs_download_url(self.session, self.owner, self.repo, oid, size)
            lfs_resp = self.session.get(lfs_url, stream=True, timeout=180)
            lfs_resp.raise_for_status()
            chunks = []
            for chunk in lfs_resp.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
            content = b"".join(chunks)

        out_path = dest_dir / rel_file
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content)
        return len(content)

    def _ensure_game_downloaded(
        self, game_number: int, game_name: str, files: list[str], force_redownload: bool
    ) -> tuple[Path, str]:
        dest_dir = GAMES_CACHE_DIR / game_name
        index_file = dest_dir / "index.html"

        if not force_redownload and index_file.is_file() and len(files) <= 1:
            self._emit_progress(
                {
                    "game_number": game_number,
                    "game_name": game_name,
                    "stage": "Loaded from local cache",
                    "current_file": "index.html",
                    "percent": 80.0,
                    "completed_files": 1,
                    "total_files": 1,
                    "speed_text": "LOCAL",
                }
            )
            return dest_dir, "Local Cache"

        if force_redownload and dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)

        total = len(files)
        completed = 0
        total_bytes = 0
        peak_bps = 0.0
        start_t = time.monotonic()

        self._emit_progress(
            {
                "game_number": game_number,
                "game_name": game_name,
                "stage": f"Downloading {total} file(s)...",
                "current_file": files[0] if files else "",
                "percent": 8.0,
                "completed_files": 0,
                "total_files": total,
                "speed_text": "0.0 KB/s",
            }
        )

        workers = min(self.max_workers, max(1, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._download_single_file, game_name, f, dest_dir): f for f in files
            }
            for fut in as_completed(futures):
                rel_f = futures[fut]
                nbytes = fut.result()
                completed += 1
                total_bytes += nbytes
                elapsed = max(0.05, time.monotonic() - start_t)
                bps = total_bytes / elapsed
                if bps > peak_bps:
                    peak_bps = bps
                speed_txt = format_speed(bps)
                pct = 10.0 + (completed / max(1, total)) * 75.0
                self._emit_progress(
                    {
                        "game_number": game_number,
                        "game_name": game_name,
                        "stage": "Downloading game assets...",
                        "current_file": rel_f,
                        "percent": round(pct, 1),
                        "completed_files": completed,
                        "total_files": total,
                        "speed_text": speed_txt,
                    }
                )

        if not index_file.is_file():
            raise RuntimeError(f"Game '{game_name}' has no index.html in {self.games_folder}/.")

        with self._state_lock:
            m = self._state["metrics"]
            m["total_downloaded_bytes"] = int(m.get("total_downloaded_bytes", 0)) + total_bytes
            if peak_bps > float(m.get("peak_speed_bps", 0.0)):
                m["peak_speed_bps"] = peak_bps
            self._save_state()

        return dest_dir, "GitHub Stream"

    def _start_game_server(self, game_number: int, game_name: str, game_dir: Path) -> tuple[int, str]:
        with self._lock:
            for port, info in self._servers.items():
                if info["number"] == game_number:
                    return port, info["url"]

        preferred = BASE_PORT + game_number
        port, _ = find_free_port(preferred)
        handler = partial(GameBootHTTPHandler, directory=str(game_dir))
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/index.html"

        with self._lock:
            self._servers[port] = {
                "number": game_number,
                "name": game_name,
                "url": url,
                "server": server,
            }
        return port, url

    def stop_server(self, port: int) -> dict:
        port_int = int(port)
        with self._lock:
            info = self._servers.pop(port_int, None)
        if info and info.get("server"):
            try:
                info["server"].shutdown()
                info["server"].server_close()
            except Exception:
                pass
        return {"ok": True, "servers": self._serialize_servers()}

    def stop_all_servers(self) -> dict:
        with self._lock:
            items = list(self._servers.items())
            self._servers.clear()
        for _, info in items:
            try:
                info["server"].shutdown()
                info["server"].server_close()
            except Exception:
                pass
        return {"ok": True, "servers": []}

    def open_game_window(self, url: str, name: str, number: int) -> dict:
        def _create():
            try:
                gw = webview.create_window(
                    title=f"#{number} — {name} | CodeX-Returns",
                    url=url,
                    width=1200,
                    height=800,
                    resizable=True,
                )
                with self._lock:
                    self._game_windows.append(gw)
            except Exception:
                pass

        threading.Thread(target=_create, daemon=True).start()
        return {"ok": True}

    def launch_game_by_number(self, game_number: int, force_redownload: bool = False) -> dict:
        num = int(game_number)
        if num < 1:
            return {"ok": False, "error": "Game number must be 1 or greater.", "servers": self._serialize_servers()}

        try:
            list_res = self.get_games_list(force_refresh=False)
            if not list_res.get("ok"):
                return {
                    "ok": False,
                    "error": list_res.get("error", "Could not resolve game list."),
                    "servers": self._serialize_servers(),
                }

            target = next((g for g in list_res["games"] if g["number"] == num), None)
            if target is None:
                list_res = self.get_games_list(force_refresh=True)
                target = next((g for g in list_res.get("games", []) if g["number"] == num), None)

            if target is None:
                return {
                    "ok": False,
                    "error": f"Game #{num} was not found in {self.owner}/{self.repo} ({self.games_folder}/).",
                    "servers": self._serialize_servers(),
                }

            game_name = target["name"]
            files = target.get("files") or []
            if not files:
                files = ["index.html"]

            game_dir, source = self._ensure_game_downloaded(num, game_name, files, bool(force_redownload))
            self._emit_progress(
                {
                    "game_number": num,
                    "game_name": game_name,
                    "stage": "Starting local server...",
                    "current_file": "index.html",
                    "percent": 94.0,
                    "speed_text": "READY",
                }
            )
            port, url = self._start_game_server(num, game_name, game_dir)
            self.open_game_window(url, game_name, num)

            with self._state_lock:
                key = str(num)
                self._state["play_counts"][key] = int(self._state["play_counts"].get(key, 0)) + 1
                self._state["metrics"]["total_launches"] = int(self._state["metrics"].get("total_launches", 0)) + 1
                self._state["activity"].append(
                    {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
                        "number": num,
                        "name": game_name,
                        "port": port,
                        "source": source,
                    }
                )
                self._state["activity"] = self._state["activity"][-100:]
                self._save_state()
                updated_counts = dict(self._state["play_counts"])
                updated_activity = list(reversed(self._state["activity"]))

            self._emit_progress(
                {
                    "game_number": num,
                    "game_name": game_name,
                    "stage": "Game launched",
                    "current_file": "index.html",
                    "percent": 100.0,
                    "port": port,
                    "speed_text": "LIVE",
                }
            )
            return {
                "ok": True,
                "number": num,
                "name": game_name,
                "port": port,
                "url": url,
                "servers": self._serialize_servers(),
                "play_counts": updated_counts,
                "activity": updated_activity,
                "metrics": self._format_metrics(),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "servers": self._serialize_servers(),
            }


def main():
    hide_console_window()
    token = os.environ.get("GITHUB_TOKEN", "")
    api = LauncherAPI(token=token)
    ui_port = api.start_ui_server()
    ui_url = f"http://127.0.0.1:{ui_port}/launcher_ui.html"

    window = webview.create_window(
        title=WINDOW_TITLE,
        url=ui_url,
        js_api=api,
        width=1360,
        height=860,
        min_size=(920, 620),
        resizable=True,
    )
    api.set_window(window)
    window.events.shown += lambda: maximize_app_window(window)

    try:
        webview.start(debug=False)
    finally:
        api.stop_all_servers()
        if api._ui_server:
            try:
                api._ui_server.shutdown()
                api._ui_server.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
