"""Desktop launcher for ATLAS.

Starts the ATLAS server quietly in the background (unless one is already running), then opens ATLAS in
its own app window (Edge, Chrome, Brave or Chromium in "app" mode: no tabs, no address bar). When every
ATLAS window is closed, the server it started is shut down again.

The desktop shortcut made by the installer runs this file. You can also run it yourself:
    Windows:       .venv\\Scripts\\pythonw.exe launch.pyw
    macOS/Linux:   .venv/bin/python launch.pyw

Options:
    --keep-running   leave ATLAS running after the window closes (reminders keep firing)
    --no-window      only start the server (used for testing)
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.appwindow import find_browser as _find_browser, open_window as _open_window  # noqa: E402
from app.config import DATA_DIR  # noqa: E402  (the shared per-user folder, not the program folder)

DATA = DATA_DIR
STATE = DATA / "launcher.json"
LOG = DATA / "atlas.log"
SYSTEM = platform.system()
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
FIRST_PORT = 8765

# Talk to 127.0.0.1 directly, never through a system or corporate proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def notify(message: str) -> None:
    """Show an error to the user even when there is no console (pythonw / app bundle)."""
    try:
        if SYSTEM == "Windows":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "ATLAS", 0x10)  # type: ignore[attr-defined]
            return
        if SYSTEM == "Darwin":
            subprocess.run(
                ["osascript", "-e", "on run argv", "-e", 'display alert "ATLAS" message (item 1 of argv)', "-e", "end run", message],
                check=False, timeout=60,
            )
            return
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "ATLAS", message], check=False, timeout=10)
    except Exception:
        pass
    print(f"ATLAS: {message}", file=sys.stderr)


def is_atlas(port: int, timeout: float = 1.0) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/", timeout=timeout) as resp:
            return b'name="atlas-token"' in resp.read(65536)
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    configured = os.environ.get("ATLAS_PORT", "")
    start = int(configured) if configured.isdigit() else FIRST_PORT
    for port in range(start, start + 20):
        if port_free(port):
            return port
    raise RuntimeError(f"No free port between {start} and {start + 19}.")


def read_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def running_instance() -> int | None:
    """Port of an ATLAS server that is already up (started by an earlier launch), if any."""
    port = read_state().get("port")
    if isinstance(port, int) and is_atlas(port):
        return port
    for port in range(FIRST_PORT, FIRST_PORT + 20):  # e.g. one started with start.bat
        if not port_free(port) and is_atlas(port, timeout=0.4):
            return port
    return None


def start_server(port: int) -> subprocess.Popen:
    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ATLAS_PORT=str(port), ATLAS_OPEN_BROWSER="false", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    log = open(LOG, "a", encoding="utf-8", errors="replace")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting ATLAS on port {port} =====\n")
    log.flush()
    kwargs: dict = dict(cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    if SYSTEM == "Windows":
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, str(ROOT / "run.py")], **kwargs)
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"ATLAS stopped while starting (exit code {proc.returncode}). Details are in {LOG}")
        if is_atlas(port, timeout=0.5):
            STATE.write_text(json.dumps({"pid": proc.pid, "port": port, "started": time.time()}), encoding="utf-8")
            return proc
        time.sleep(0.3)
    stop_server(proc)
    raise RuntimeError(f"ATLAS didn't start within 90 seconds. Details are in {LOG}")


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if SYSTEM == "Windows":
            # /T also closes child processes, such as the browser ATLAS drives for bookings.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGTERM)  # graceful: lets ATLAS close its browser and connections
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        if read_state().get("pid") == proc.pid:
            STATE.unlink()
    except OSError:
        pass


def find_browser() -> str | None:
    return _find_browser()


def open_window(url: str) -> subprocess.Popen | None:
    return _open_window(url, DATA / "app-window")


def main() -> int:
    keep_running = "--keep-running" in sys.argv or os.environ.get("ATLAS_KEEP_RUNNING", "").lower() in {"1", "true", "yes"}
    no_window = "--no-window" in sys.argv
    os.chdir(ROOT)
    server = None
    try:
        port = running_instance()
        if port is None:
            port = pick_port()
            server = start_server(port)
    except Exception as exc:
        notify(str(exc))
        return 1
    url = f"http://127.0.0.1:{port}/"
    if no_window:
        print(url, flush=True)
        if server is not None:
            try:
                server.wait()
            except KeyboardInterrupt:
                stop_server(server)
        return 0

    window = open_window(url)
    if window is None or server is None or keep_running:
        return 0  # default browser tab, or ATLAS was already running: leave the server as it is
    opened = time.time()
    try:
        window.wait()
    except KeyboardInterrupt:
        pass
    if time.time() - opened < 5:
        # The browser handed the window to an instance that was already open, so we can't tell when it
        # closes. Keep ATLAS running; the next launch reuses it.
        return 0
    stop_server(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
