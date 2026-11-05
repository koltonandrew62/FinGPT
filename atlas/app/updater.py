"""In-app updates: check GitHub for a newer ATLAS and install it into this program folder.

Only the program files are replaced. Settings (.env) and data live in the per-user folder (config.USER_DIR), so
an update never touches them. A copy that is a git checkout is never overwritten (use git pull there).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .config import ROOT
from .version import VERSION

REPO = os.environ.get("ATLAS_UPDATE_REPO", "koltonandrew62/FinGPT")
BRANCH = os.environ.get("ATLAS_UPDATE_BRANCH", "claude/eloquent-einstein-uqi56u")
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/refs/heads/{BRANCH}/atlas/app/version.py"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
MAX_ZIP_BYTES = 150 * 1024 * 1024
SKIP = {".env", "data", ".venv", "__pycache__", ".git"}
CREATE_NO_WINDOW = 0x08000000
_VERSION_RE = re.compile(r'^VERSION\s*=\s*["\'](\d+(?:\.\d+){0,3})["\']', re.M)

_cache: dict = {"at": 0.0, "result": None}
_lock = asyncio.Lock()
_state: dict = {"status": "idle", "message": "", "version": None}


class UpdateError(Exception):
    pass


def parse_version(text: str) -> str | None:
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def is_newer(latest: str | None, current: str = VERSION) -> bool:
    try:
        return bool(latest) and _tuple(latest) > _tuple(current)
    except ValueError:
        return False


def is_git_checkout(root: Path = ROOT) -> bool:
    return (root / ".git").exists() or (root.parent / ".git").exists()


async def check(http, force: bool = False) -> dict:
    """{current, latest, update_available, can_update, error} — cached for an hour."""
    if not force and _cache["result"] and time.time() - _cache["at"] < 3600:
        return {**_cache["result"], "install": dict(_state)}
    result = {"current": VERSION, "latest": None, "update_available": False, "can_update": not is_git_checkout(), "error": None}
    try:
        r = await http.get(VERSION_URL, timeout=15, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        latest = parse_version(r.text)
        if not latest:
            raise UpdateError("couldn't read the latest version number")
        result.update(latest=latest, update_available=is_newer(latest))
    except Exception as exc:  # offline, GitHub down, ...
        result["error"] = f"Couldn't check for updates ({exc.__class__.__name__})."
    if not result["can_update"]:
        result["note"] = "This copy is a git checkout: update it with git pull."
    _cache.update(at=time.time(), result=result)
    return {**result, "install": dict(_state)}


def _safe_members(zf: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """The archive's atlas/ folder prefix and its members, refusing absolute or '..' paths."""
    prefix = None
    for info in zf.infolist():
        parts = PurePosixPath(info.filename).parts
        if len(parts) >= 3 and parts[1] == "atlas" and parts[2] == "run.py":
            prefix = f"{parts[0]}/atlas/"
            break
    if not prefix:
        raise UpdateError("The download doesn't contain the atlas folder.")
    members = []
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(prefix) or name == prefix:
            continue
        rel = PurePosixPath(name[len(prefix):])
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] in SKIP:
            continue
        members.append(info)
    return prefix, members


def _pip_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def _install(zip_path: Path, root: Path = ROOT) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        prefix, members = _safe_members(zf)
        new_version = None
        try:
            new_version = parse_version(zf.read(prefix + "app/version.py").decode("utf-8", "replace"))
        except KeyError:
            pass
        staging = Path(tempfile.mkdtemp(prefix="atlas-update-"))
        try:
            for info in members:
                rel = PurePosixPath(info.filename[len(prefix):])
                target = staging.joinpath(*rel.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            if not (staging / "run.py").is_file() or not (staging / "app" / "server.py").is_file():
                raise UpdateError("The download looks incomplete.")
            # Copy over the program folder (never .env, data, .venv).
            for item in staging.iterdir():
                dest = root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    req = root / "requirements.txt"
    if req.is_file():
        kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
        res = subprocess.run(
            [_pip_python(), "-m", "pip", "install", "-q", "-r", str(req), "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=900, **kwargs,
        )
        if res.returncode != 0:
            raise UpdateError("The new files are in place, but installing components failed. Check your internet connection and try again.")
    return new_version or "the latest version"


async def apply(http) -> dict:
    """Download the latest version from GitHub and install it over this copy."""
    if is_git_checkout():
        raise UpdateError("This copy is a git checkout: update it with git pull.")
    if _lock.locked():
        raise UpdateError("An update is already running.")
    async with _lock:
        _state.update(status="downloading", message="Downloading the update…", version=None)
        tmp = Path(tempfile.mkdtemp(prefix="atlas-dl-"))
        try:
            zip_path = tmp / "atlas.zip"
            size = 0
            async with http.stream("GET", ZIP_URL, timeout=120, follow_redirects=True) as r:
                if r.status_code != 200:
                    raise UpdateError(f"Download failed (HTTP {r.status_code}).")
                with open(zip_path, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_ZIP_BYTES:
                            raise UpdateError("The download is unexpectedly large; stopped.")
                        fh.write(chunk)
            _state.update(status="installing", message="Installing…")
            version = await asyncio.to_thread(_install, zip_path)
        except UpdateError as exc:
            _state.update(status="error", message=str(exc))
            raise
        except Exception as exc:
            _state.update(status="error", message=f"Update failed ({exc.__class__.__name__}).")
            raise UpdateError(_state["message"]) from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _cache.update(at=0.0, result=None)
        _state.update(status="done", version=version, message=f"Updated to {version}. Close and reopen to finish.")
        return {"status": "updated", "version": version, "restart_required": True, "message": _state["message"]}
