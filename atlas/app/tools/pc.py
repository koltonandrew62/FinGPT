"""Control of the local computer: apps, web, media, volume, system status, files, shell, screen, power.

Everything here runs on the machine hosting the ATLAS server. Anything with side effects is listed in
config.SENSITIVE_TOOLS so the UI asks the user to approve it first.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import io
import os
import platform
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from ..util import clip
from . import ToolError, tool

SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"
IS_WIN, IS_MAC = SYSTEM == "Windows", SYSTEM == "Darwin"
CREATE_NO_WINDOW = 0x08000000

# alias -> (windows target, macOS app name, linux commands, windows process image for closing)
APPS: dict[str, tuple[str, str, list[str], str]] = {
    "chrome": ("chrome", "Google Chrome", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"], "chrome.exe"),
    "edge": ("msedge", "Microsoft Edge", ["microsoft-edge"], "msedge.exe"),
    "firefox": ("firefox", "Firefox", ["firefox"], "firefox.exe"),
    "safari": ("", "Safari", [], ""),
    "spotify": ("spotify:", "Spotify", ["spotify"], "Spotify.exe"),
    "discord": ("discord:", "Discord", ["discord"], "Discord.exe"),
    "steam": ("steam:", "Steam", ["steam"], "steam.exe"),
    "slack": ("slack:", "Slack", ["slack"], "slack.exe"),
    "zoom": ("zoommtg:", "zoom.us", ["zoom"], "Zoom.exe"),
    "teams": ("msteams:", "Microsoft Teams", ["teams"], "ms-teams.exe"),
    "vscode": ("code", "Visual Studio Code", ["code"], "Code.exe"),
    "notepad": ("notepad", "TextEdit", ["gedit", "gnome-text-editor", "kate", "mousepad"], "notepad.exe"),
    "calculator": ("calc", "Calculator", ["gnome-calculator", "kcalc", "galculator"], "CalculatorApp.exe"),
    "terminal": ("wt", "Terminal", ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"], "WindowsTerminal.exe"),
    "file explorer": ("explorer", "Finder", ["xdg-open ~", "nautilus", "dolphin", "thunar"], "explorer.exe"),
    "task manager": ("taskmgr", "Activity Monitor", ["gnome-system-monitor", "ksysguard"], "Taskmgr.exe"),
    "settings": ("ms-settings:", "System Settings", ["gnome-control-center", "systemsettings"], "SystemSettings.exe"),
    "word": ("winword", "Microsoft Word", ["libreoffice --writer"], "WINWORD.EXE"),
    "excel": ("excel", "Microsoft Excel", ["libreoffice --calc"], "EXCEL.EXE"),
    "powerpoint": ("powerpnt", "Microsoft PowerPoint", ["libreoffice --impress"], "POWERPNT.EXE"),
    "outlook": ("outlook", "Microsoft Outlook", ["thunderbird"], "OUTLOOK.EXE"),
    "paint": ("mspaint", "Preview", ["pinta", "kolourpaint"], "mspaint.exe"),
    "camera": ("microsoft.windows.camera:", "Photo Booth", ["cheese"], "WindowsCamera.exe"),
    "music": ("mswindowsmusic:", "Music", ["rhythmbox"], ""),
}
ALIASES = {
    "google chrome": "chrome", "browser": "chrome", "microsoft edge": "edge", "code": "vscode",
    "visual studio code": "vscode", "vs code": "vscode", "explorer": "file explorer", "finder": "file explorer",
    "files": "file explorer", "calc": "calculator", "cmd": "terminal", "command prompt": "terminal",
    "powershell": "terminal", "activity monitor": "task manager", "system settings": "settings",
    "control panel": "settings", "microsoft word": "word", "microsoft excel": "excel", "textedit": "notepad",
    "text editor": "notepad", "microsoft teams": "teams",
}


def _resolve_app(name: str):
    key = name.lower().strip()
    key = ALIASES.get(key, key)
    if key in APPS:
        return key, APPS[key]
    for alias, entry in APPS.items():
        if key in alias or alias in key:
            return alias, entry
    return key, None


def _detach(args, shell: bool = False):
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=shell)
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def _run(args, timeout: float = 15) -> subprocess.CompletedProcess:
    kwargs = dict(capture_output=True, text=True, timeout=timeout)
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def _osascript(script: str) -> str:
    res = _run(["osascript", "-e", script])
    if res.returncode != 0:
        raise ToolError(res.stderr.strip() or "AppleScript failed.")
    return res.stdout.strip()


def _win_start_menu_lookup(name: str) -> str | None:
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    wanted = name.lower()
    best = None
    for root in roots:
        if not root.exists():
            continue
        for lnk in root.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if stem == wanted:
                return str(lnk)
            if wanted in stem and (best is None or len(stem) < len(Path(best).stem)):
                best = str(lnk)
    return best


def _open_app_sync(name: str) -> str:
    key, entry = _resolve_app(name)
    if IS_WIN:
        targets = [t for t in ((entry[0] if entry else ""), name) if t]
        for target in targets:
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return f"Opened {key}."
            except OSError:
                continue
        lnk = _win_start_menu_lookup(name)
        if lnk:
            os.startfile(lnk)  # type: ignore[attr-defined]
            return f"Opened {Path(lnk).stem}."
        raise ToolError(f"I couldn't find an app called '{name}' on this PC.")
    if IS_MAC:
        app = entry[1] if entry and entry[1] else name
        res = _run(["open", "-a", app])
        if res.returncode != 0:
            raise ToolError(f"macOS couldn't open '{app}': {res.stderr.strip()}")
        return f"Opened {app}."
    # Linux
    candidates = (entry[2] if entry else []) + [name.lower().replace(" ", "-"), name.lower().replace(" ", "")]
    for cmd in candidates:
        parts = cmd.split()
        if shutil.which(parts[0]):
            parts = [os.path.expanduser(p) for p in parts]
            _detach(parts)
            return f"Opened {key}."
    if shutil.which("gtk-launch"):
        res = _run(["gtk-launch", name.lower()])
        if res.returncode == 0:
            return f"Opened {name}."
    raise ToolError(f"I couldn't find an app called '{name}' on this computer.")


@tool(
    "open_application",
    "Open (launch) an application on the user's computer by name, e.g. 'Spotify', 'Chrome', 'VS Code', "
    "'Calculator', 'File Explorer', 'Discord', 'Settings', 'Word'.",
    {"name": {"type": "string"}},
    ["name"],
    label="Launching application",
)
async def open_application(ctx, name: str):
    return {"status": await asyncio.to_thread(_open_app_sync, name)}


def _close_preview(args):
    return {"title": "Close application", "fields": [["App", args.get("name", "")]], "body": "Unsaved work in that app may be lost."}


@tool(
    "close_application",
    "Close (quit) a running application by name.",
    {"name": {"type": "string"}},
    ["name"],
    label="Closing application",
    preview=_close_preview,
)
async def close_application(ctx, name: str):
    key, entry = _resolve_app(name)

    def work():
        if IS_WIN:
            image = (entry[3] if entry and entry[3] else (name if name.lower().endswith(".exe") else f"{name}.exe"))
            res = _run(["taskkill", "/IM", image, "/F"])
            if res.returncode != 0:
                raise ToolError(res.stderr.strip() or res.stdout.strip() or f"Couldn't close {image}.")
            return f"Closed {image}."
        if IS_MAC:
            app = entry[1] if entry and entry[1] else name
            _osascript(f'quit app "{app}"')
            return f"Closed {app}."
        procs = entry[2] if entry else [name]
        for p in procs + [name]:
            res = _run(["pkill", "-f", p.split()[0]])
            if res.returncode == 0:
                return f"Closed {p.split()[0]}."
        raise ToolError(f"No running process matched '{name}'.")

    return {"status": await asyncio.to_thread(work)}


@tool(
    "open_website",
    "Open a URL in the user's default browser, or run a Google search if given plain words.",
    {"url_or_query": {"type": "string"}},
    ["url_or_query"],
    label="Opening browser",
)
async def open_website(ctx, url_or_query: str):
    target = url_or_query.strip()
    if " " not in target and "." in target and not target.startswith(("http://", "https://")):
        target = "https://" + target
    if not target.startswith(("http://", "https://")):
        target = "https://www.google.com/search?q=" + quote_plus(url_or_query)
    await asyncio.to_thread(webbrowser.open, target)
    return {"opened": target}


@tool(
    "play_media",
    "Find and play music or video: opens a YouTube search or Spotify search for the query.",
    {
        "query": {"type": "string", "description": "Song, artist, playlist, video or podcast."},
        "platform": {"type": "string", "enum": ["youtube", "spotify"], "description": "Default 'youtube'."},
    },
    ["query"],
    label="Cueing media",
)
async def play_media(ctx, query: str, platform: str = "youtube"):
    if platform == "spotify":
        uri = f"spotify:search:{quote_plus(query)}"

        def go():
            try:
                if IS_WIN:
                    os.startfile(uri)  # type: ignore[attr-defined]
                elif IS_MAC:
                    if _run(["open", uri]).returncode != 0:
                        raise OSError
                else:
                    if not shutil.which("xdg-open") or _run(["xdg-open", uri]).returncode != 0:
                        raise OSError
                return uri
            except OSError:
                url = f"https://open.spotify.com/search/{quote_plus(query)}"
                webbrowser.open(url)
                return url

        return {"opened": await asyncio.to_thread(go)}
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    await asyncio.to_thread(webbrowser.open, url)
    return {"opened": url}


# ------------------------------------------------------------------ media/volume
VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF}


def _win_keys(vk: int, times: int = 1):
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for _ in range(times):
        user32.keybd_event(vk, 0, 0x1, 0)
        user32.keybd_event(vk, 0, 0x1 | 0x2, 0)
        time.sleep(0.01)


def _mac_player() -> str:
    try:
        if _osascript('application "Spotify" is running') == "true":
            return "Spotify"
    except ToolError:
        pass
    return "Music"


def _media_sync(action: str, steps: int) -> str:
    if IS_WIN:
        _win_keys(VK[action], steps if action.startswith("volume") else 1)
        return f"{action.replace('_', ' ')} done"
    if IS_MAC:
        if action == "volume_up":
            _osascript(f"set volume output volume ((output volume of (get volume settings)) + {steps * 2})")
        elif action == "volume_down":
            _osascript(f"set volume output volume ((output volume of (get volume settings)) - {steps * 2})")
        elif action == "mute":
            _osascript("set volume output muted (not (output muted of (get volume settings)))")
        else:
            cmd = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}[action]
            player = _mac_player()
            _osascript(f'tell application "{player}" to {cmd}')
        return f"{action.replace('_', ' ')} done"
    # Linux
    if action.startswith("volume") or action == "mute":
        if shutil.which("pactl"):
            if action == "mute":
                args = ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]
            else:
                sign = "+" if action == "volume_up" else "-"
                args = ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{steps * 2}%"]
        elif shutil.which("amixer"):
            args = ["amixer", "-q", "sset", "Master", "toggle" if action == "mute" else f"{steps * 2}%{'+' if action == 'volume_up' else '-'}"]
        else:
            raise ToolError("Install pactl or amixer for volume control.")
        _run(args)
        return f"{action.replace('_', ' ')} done"
    if not shutil.which("playerctl"):
        raise ToolError("Install playerctl for media control on Linux.")
    _run(["playerctl", {"play_pause": "play-pause", "next": "next", "previous": "previous"}[action]])
    return f"{action.replace('_', ' ')} done"


@tool(
    "media_control",
    "Control media playback and volume: play/pause, next/previous track, mute toggle, volume up/down.",
    {
        "action": {"type": "string", "enum": list(VK)},
        "steps": {"type": "integer", "description": "For volume up/down: number of 2% steps. Default 5 (10%)."},
    },
    ["action"],
    label="Adjusting media",
)
async def media_control(ctx, action: str, steps: int = 5):
    if action not in VK:
        raise ToolError(f"Unknown action '{action}'.")
    return {"status": await asyncio.to_thread(_media_sync, action, max(1, min(int(steps or 5), 50)))}


def _set_volume_sync(level: int) -> str:
    if IS_WIN:
        _win_keys(VK["volume_down"], 50)
        _win_keys(VK["volume_up"], round(level / 2))
    elif IS_MAC:
        _osascript(f"set volume output volume {level}")
    elif shutil.which("pactl"):
        _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    elif shutil.which("amixer"):
        _run(["amixer", "-q", "sset", "Master", f"{level}%"])
    else:
        raise ToolError("No volume control utility found.")
    return f"Volume set to {level}%."


@tool(
    "set_volume",
    "Set the system output volume to an exact level 0-100.",
    {"level": {"type": "integer"}},
    ["level"],
    label="Setting volume",
)
async def set_volume(ctx, level: int):
    return {"status": await asyncio.to_thread(_set_volume_sync, max(0, min(int(level), 100)))}


# ------------------------------------------------------------------ system info
def system_snapshot() -> dict:
    try:
        import psutil
    except ImportError:
        return {"os": f"{SYSTEM} {platform.release()}", "error": "psutil not installed"}
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        disk_info = {"percent": disk.percent, "free_gb": round(disk.free / 1e9, 1), "total_gb": round(disk.total / 1e9, 1)}
    except OSError:
        disk_info = None
    batt = None
    try:
        b = psutil.sensors_battery()
        if b:
            batt = {"percent": round(b.percent), "plugged_in": b.power_plugged, "minutes_left": None if b.secsleft in (-1, -2) or b.secsleft is None else int(b.secsleft / 60)}
    except Exception:
        pass
    return {
        "os": f"{SYSTEM} {platform.release()}",
        "host": platform.node(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(),
        "memory": {"percent": vm.percent, "used_gb": round(vm.used / 1e9, 1), "total_gb": round(vm.total / 1e9, 1)},
        "disk": disk_info,
        "battery": batt,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    }


@tool(
    "system_status",
    "Report the computer's health: CPU, memory, disk, battery, uptime and the heaviest running processes.",
    {},
    label="Running diagnostics",
)
async def system_status(ctx):
    def work():
        snap = system_snapshot()
        try:
            import psutil

            psutil.cpu_percent(interval=0.4)
            snap["cpu_percent"] = psutil.cpu_percent(interval=None)
            procs = []
            for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
                try:
                    procs.append((p.info["memory_info"].rss if p.info["memory_info"] else 0, p.info["name"]))
                except Exception:
                    continue
            procs.sort(reverse=True)
            snap["top_memory_processes"] = [{"name": n, "memory_mb": round(m / 1e6)} for m, n in procs[:6]]
        except Exception:
            pass
        return snap

    return await asyncio.to_thread(work)


# ------------------------------------------------------------------ screen
def _grab():
    try:
        from PIL import ImageGrab
    except ImportError:
        raise ToolError("Screen capture needs Pillow: pip install pillow")
    try:
        return ImageGrab.grab(all_screens=True) if IS_WIN else ImageGrab.grab()
    except Exception as exc:
        raise ToolError(f"Couldn't capture the screen: {exc}")


@tool(
    "take_screenshot",
    "Capture the screen and save it as a PNG in the user's Pictures/ATLAS folder.",
    {},
    label="Capturing screen",
)
async def take_screenshot(ctx):
    def work():
        img = _grab()
        folder = Path.home() / "Pictures" / "ATLAS"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        img.save(path)
        return {"saved": str(path), "size": f"{img.width}x{img.height}"}

    return await asyncio.to_thread(work)


@tool(
    "look_at_screen",
    "Look at what is currently on the user's screen and answer a question about it "
    "(read an error message, summarize a page, describe an image, help with what they're doing).",
    {"question": {"type": "string", "description": "What to look for. Default: describe the screen."}},
    label="Analyzing screen",
    timeout=120,
)
async def look_at_screen(ctx, question: str = "Describe what is on the screen in detail."):
    def capture() -> str:
        img = _grab().convert("RGB")
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode()

    b64 = await asyncio.to_thread(capture)
    model = await ctx.llm.resolve_model()
    data = await ctx.llm.complete(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"This is a screenshot of the user's computer screen. {question} Be specific and concise; quote any visible error text exactly."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        model=model,
        max_tokens=900,
    )
    return {"observation": ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")}


# ------------------------------------------------------------------ files
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "AppData", "Library", ".cache", "venv", ".venv", "site-packages", "$Recycle.Bin"}


def _expand(path: str) -> Path:
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    special = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures", "music": "Music", "videos": "Videos"}
    if path.lower() in special:
        return Path.home() / special[path.lower()]
    p = Path(path)
    return p if p.is_absolute() else Path.home() / p


@tool(
    "find_files",
    "Search the user's files by name (substring or wildcard like '*.pdf'). Searches the home folder by default.",
    {
        "name": {"type": "string", "description": "Part of the filename or a glob, e.g. 'resume', '*.xlsx'"},
        "folder": {"type": "string", "description": "Where to search: 'Desktop', 'Documents', 'Downloads' or a path. Default home."},
    },
    ["name"],
    label="Searching files",
)
async def find_files(ctx, name: str, folder: str = "~"):
    root = _expand(folder or "~")
    if not root.exists():
        raise ToolError(f"Folder not found: {root}")
    pattern = name.lower()
    is_glob = any(ch in pattern for ch in "*?[")

    def work():
        found, deadline = [], time.time() + 6
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for fn in filenames:
                low = fn.lower()
                if (fnmatch.fnmatch(low, pattern) if is_glob else pattern in low):
                    full = Path(dirpath) / fn
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    found.append((st.st_mtime, str(full), st.st_size))
            if time.time() > deadline or len(found) > 400:
                break
        found.sort(reverse=True)
        return {
            "searched": str(root),
            "matches": [
                {"path": p, "size_kb": round(s / 1024, 1), "modified": datetime.fromtimestamp(m).strftime("%Y-%m-%d %H:%M")}
                for m, p, s in found[:25]
            ],
            "truncated": len(found) > 25,
        }

    return await asyncio.to_thread(work)


@tool(
    "open_path",
    "Open a file or folder with its default app (e.g. open a PDF, a photo, or the Downloads folder).",
    {"path": {"type": "string"}},
    ["path"],
    label="Opening file",
)
async def open_path(ctx, path: str):
    p = _expand(path)
    if not p.exists():
        raise ToolError(f"Not found: {p}")

    def go():
        if IS_WIN:
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif IS_MAC:
            _run(["open", str(p)])
        else:
            _detach(["xdg-open", str(p)])

    await asyncio.to_thread(go)
    return {"opened": str(p)}


def _read_preview(args):
    return {"title": "Read file", "fields": [["Path", str(_expand(args.get("path", "")))]], "body": "The contents will be sent to the AI model to answer you."}


@tool(
    "read_file",
    "Read a text file (or list a folder) on the user's computer.",
    {"path": {"type": "string"}},
    ["path"],
    label="Reading file",
    preview=_read_preview,
)
async def read_file(ctx, path: str):
    p = _expand(path)
    if not p.exists():
        raise ToolError(f"Not found: {p}")
    if p.is_dir():
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:150]
        return {"folder": str(p), "entries": [e.name + ("/" if e.is_dir() else "") for e in entries]}
    if p.stat().st_size > 5_000_000:
        raise ToolError("That file is too large to read (over 5 MB).")
    if p.suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".mp3", ".mp4"}:
        raise ToolError(f"I can't read {p.suffix} files as text. Use open_path to open it instead.")
    text = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
    return {"path": str(p), "content": clip(text, 15000)}


def _write_preview(args):
    mode = args.get("mode", "overwrite")
    return {
        "title": "Append to file" if mode == "append" else "Write file",
        "fields": [["Path", str(_expand(args.get("path", "")))], ["Mode", mode]],
        "body": clip(args.get("content", ""), 3000),
    }


@tool(
    "write_file",
    "Create or overwrite a text file (or append to one) on the user's computer, e.g. save a list to the Desktop.",
    {
        "path": {"type": "string", "description": "e.g. 'Desktop/groceries.txt'"},
        "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["overwrite", "append"]},
    },
    ["path", "content"],
    label="Writing file",
    preview=_write_preview,
)
async def write_file(ctx, path: str, content: str, mode: str = "overwrite"):
    p = _expand(path)

    def work():
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if mode == "append" else "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"written": str(p), "bytes": len(content.encode())}

    return await asyncio.to_thread(work)


# ------------------------------------------------------------------ clipboard
@tool(
    "clipboard",
    "Read the clipboard text, or copy text to the clipboard.",
    {"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": "string"}},
    ["action"],
    label="Using clipboard",
)
async def clipboard(ctx, action: str, text: str = ""):
    def work():
        if action == "write":
            if IS_WIN:
                subprocess.run(["clip"], input=text.encode("utf-16le"), check=True, creationflags=CREATE_NO_WINDOW)
            elif IS_MAC:
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
            elif shutil.which("wl-copy"):
                subprocess.run(["wl-copy"], input=text.encode(), check=True)
            elif shutil.which("xclip"):
                subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
            else:
                raise ToolError("Install xclip or wl-clipboard for clipboard access.")
            return {"copied_chars": len(text)}
        if IS_WIN:
            res = _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])
        elif IS_MAC:
            res = _run(["pbpaste"])
        elif shutil.which("wl-paste"):
            res = _run(["wl-paste", "--no-newline"])
        elif shutil.which("xclip"):
            res = _run(["xclip", "-selection", "clipboard", "-o"])
        else:
            raise ToolError("Install xclip or wl-clipboard for clipboard access.")
        return {"clipboard": clip(res.stdout, 8000)}

    return await asyncio.to_thread(work)


# ------------------------------------------------------------------ keyboard
def _pyautogui():
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        return pyautogui
    except Exception as exc:
        raise ToolError(f"Keyboard control needs pyautogui (pip install pyautogui): {exc}")


@tool(
    "type_text",
    "Type text into whatever window currently has focus, as if on the keyboard.",
    {"text": {"type": "string"}},
    ["text"],
    label="Typing",
    preview=lambda a: {"title": "Type into the focused window", "fields": [], "body": clip(a.get("text", ""), 2000)},
)
async def type_text(ctx, text: str):
    pg = _pyautogui()
    await asyncio.to_thread(pg.write, text, 0.01)
    return {"typed_chars": len(text)}


@tool(
    "press_keys",
    "Press a key or shortcut in the focused window, e.g. 'enter', 'ctrl+s', 'alt+tab', 'win+d', 'cmd+space'.",
    {"keys": {"type": "string"}},
    ["keys"],
    label="Pressing keys",
    preview=lambda a: {"title": "Press keys", "fields": [["Keys", a.get("keys", "")]], "body": ""},
)
async def press_keys(ctx, keys: str):
    pg = _pyautogui()
    combo = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    mapping = {"win": "winleft", "windows": "winleft", "cmd": "command", "control": "ctrl", "return": "enter", "escape": "esc"}
    combo = [mapping.get(k, k) for k in combo]
    if not combo:
        raise ToolError("No keys given.")
    await asyncio.to_thread(pg.hotkey, *combo) if len(combo) > 1 else await asyncio.to_thread(pg.press, combo[0])
    return {"pressed": "+".join(combo)}


# ------------------------------------------------------------------ shell & power
def _shell_args(command: str) -> list[str]:
    if IS_WIN:
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    shell = "/bin/zsh" if IS_MAC and os.path.exists("/bin/zsh") else ("/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh")
    return [shell, "-lc", command]


@tool(
    "run_command",
    "Run a shell command on the user's computer and return its output (PowerShell on Windows, zsh/bash "
    "elsewhere). Use for things no other tool covers: checking IP/network, installed versions, git, "
    "batch file operations, etc. Prefer safe, read-only commands; explain destructive ones.",
    {
        "command": {"type": "string"},
        "working_directory": {"type": "string", "description": "Optional folder to run in."},
        "timeout_seconds": {"type": "integer", "description": "Default 60, max 300."},
    },
    ["command"],
    label="Executing command",
    preview=lambda a: {
        "title": "Run command",
        "fields": [["Shell", "PowerShell" if IS_WIN else "shell"]] + ([["In", a["working_directory"]]] if a.get("working_directory") else []),
        "body": a.get("command", ""),
        "code": True,
    },
    timeout=320,
)
async def run_command(ctx, command: str, working_directory: str | None = None, timeout_seconds: int = 60):
    if not ctx.settings.allow_shell:
        raise ToolError("Shell commands are disabled (ALLOW_SHELL=false).")
    cwd = str(_expand(working_directory)) if working_directory else str(Path.home())
    if not Path(cwd).is_dir():
        raise ToolError(f"Folder not found: {cwd}")
    timeout = max(5, min(int(timeout_seconds or 60), 300))

    def work():
        try:
            res = subprocess.run(
                _shell_args(command), capture_output=True, text=True, timeout=timeout, cwd=cwd,
                **({"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}),
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout}s.")
        return {"exit_code": res.returncode, "stdout": res.stdout[-8000:], "stderr": res.stderr[-3000:]}

    return await asyncio.to_thread(work)


@tool(
    "lock_screen",
    "Lock the computer's screen.",
    {},
    label="Locking workstation",
)
async def lock_screen(ctx):
    def go():
        if IS_WIN:
            import ctypes

            ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
        elif IS_MAC:
            res = _run(["pmset", "displaysleepnow"])
            if res.returncode != 0:
                raise ToolError("Couldn't lock the screen.")
        else:
            for args in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]):
                if shutil.which(args[0]) and _run(args).returncode == 0:
                    return
            raise ToolError("No screen-lock command available.")

    await asyncio.to_thread(go)
    return {"status": "locked"}


POWER = {
    "shutdown": {"Windows": ["shutdown", "/s", "/t", "15"], "Darwin": 'tell application "System Events" to shut down', "Linux": ["systemctl", "poweroff"]},
    "restart": {"Windows": ["shutdown", "/r", "/t", "15"], "Darwin": 'tell application "System Events" to restart', "Linux": ["systemctl", "reboot"]},
    "sleep": {"Windows": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], "Darwin": 'tell application "System Events" to sleep', "Linux": ["systemctl", "suspend"]},
    "log_out": {"Windows": ["shutdown", "/l"], "Darwin": 'tell application "System Events" to log out', "Linux": ["loginctl", "terminate-user", os.environ.get("USER", "")]},
    "cancel_shutdown": {"Windows": ["shutdown", "/a"], "Darwin": None, "Linux": ["shutdown", "-c"]},
}


@tool(
    "power_action",
    "Shut down, restart, sleep or log out of the computer (or cancel a pending Windows shutdown).",
    {"action": {"type": "string", "enum": list(POWER)}},
    ["action"],
    label="Power control",
    preview=lambda a: {"title": f"Power: {a.get('action', '').replace('_', ' ')}", "fields": [], "body": "Save your work first."},
)
async def power_action(ctx, action: str):
    spec = POWER.get(action, {}).get(SYSTEM)
    if spec is None:
        raise ToolError(f"'{action}' isn't supported on {SYSTEM}.")

    def go():
        if isinstance(spec, str):
            _osascript(spec)
        else:
            res = _run(spec)
            if res.returncode != 0:
                raise ToolError(res.stderr.strip() or f"{action} failed.")

    await asyncio.to_thread(go)
    return {"status": f"{action} initiated"}


def capabilities() -> dict:
    """What optional PC features work on this machine (shown in the UI)."""
    caps = {"platform": SYSTEM, "python": sys.version.split()[0]}
    try:
        import psutil  # noqa: F401

        caps["system_stats"] = True
    except ImportError:
        caps["system_stats"] = False
    try:
        from PIL import ImageGrab  # noqa: F401

        caps["screen"] = True
    except ImportError:
        caps["screen"] = False
    try:
        import importlib.util

        caps["keyboard"] = importlib.util.find_spec("pyautogui") is not None
    except Exception:
        caps["keyboard"] = False
    return caps
