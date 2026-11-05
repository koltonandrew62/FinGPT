"""Open ATLAS in its own app window (Edge / Chrome / Brave / Chromium "app" mode: no tabs, no address bar).

Shared by launch.pyw (desktop shortcut) and run.py / start.bat. These browsers also support the speech
recognition that voice-only mode needs; a default browser such as Firefox does not.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import webbrowser
from pathlib import Path

SYSTEM = platform.system()
CREATE_NO_WINDOW = 0x08000000


def find_browser() -> str | None:
    override = os.environ.get("ATLAS_APP_BROWSER", "")
    if override and Path(override).exists():
        return override
    if SYSTEM == "Windows":
        rel = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            for r in rel:
                if base and (Path(base) / r).exists():
                    return str(Path(base) / r)
        return shutil.which("msedge") or shutil.which("chrome")
    if SYSTEM == "Darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            for folder in (Path("/Applications"), Path.home() / "Applications"):
                exe = folder / f"{app}.app" / "Contents" / "MacOS" / app
                if exe.exists():
                    return str(exe)
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_window(url: str, profile: Path) -> subprocess.Popen | None:
    """Open the app window; falls back to the default browser (returns None then)."""
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile.mkdir(parents=True, exist_ok=True)  # its own profile: a separate app that remembers mic permission
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1440,900",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if SYSTEM == "Windows":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError:
        webbrowser.open(url)
        return None
