"""PC-control tools, exercised without touching the real desktop (browser, keyboard and power are faked)."""
import json
from pathlib import Path

import pytest

from app.tools import call_tool, pc
from conftest import make_ctx, run


def tool(_tool_name, ctx=None, **args):
    return json.loads(run(call_tool(_tool_name, args, ctx or make_ctx())))


def test_resolve_app_aliases():
    assert pc._resolve_app("Google Chrome")[0] == "chrome"
    assert pc._resolve_app("VS Code")[0] == "vscode"
    assert pc._resolve_app("  Finder ")[0] == "file explorer"
    assert pc._resolve_app("spotify premium")[0] == "spotify"
    key, entry = pc._resolve_app("Obscure Thing")
    assert key == "obscure thing" and entry is None


def test_expand_paths():
    assert pc._expand("desktop") == Path.home() / "Desktop"
    assert pc._expand("Downloads") == Path.home() / "Downloads"
    assert pc._expand("notes/todo.txt") == Path.home() / "notes/todo.txt"
    assert pc._expand('"/tmp/x y.txt"') == Path("/tmp/x y.txt")
    assert pc._expand("~/a") == Path.home() / "a"


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(pc.webbrowser, "open", lambda url, *a, **k: urls.append(url) or True)
    return urls


def test_open_website_and_media(opened):
    assert tool("open_website", url_or_query="github.com") == {"opened": "https://github.com"}
    assert tool("open_website", url_or_query="https://example.com/a b")["opened"] == "https://example.com/a b"
    assert tool("open_website", url_or_query="best pizza near me")["opened"] == "https://www.google.com/search?q=best+pizza+near+me"
    assert tool("open_website", url_or_query="javascript:alert(1)")["opened"].startswith("https://www.google.com/search?q=")
    assert tool("play_media", query="lofi beats")["opened"] == "https://www.youtube.com/results?search_query=lofi+beats"
    assert len(opened) == 5


class FakePyAutoGUI:
    def __init__(self):
        self.calls = []

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def press(self, key):
        self.calls.append(("press", key))

    def write(self, text, interval=0):
        self.calls.append(("write", text))


def test_keyboard_tools(monkeypatch):
    fake = FakePyAutoGUI()
    monkeypatch.setattr(pc, "_pyautogui", lambda: fake)
    assert tool("press_keys", keys="Ctrl + S") == {"pressed": "ctrl+s"}
    assert tool("press_keys", keys="win+d") == {"pressed": "winleft+d"}
    assert tool("press_keys", keys="Return") == {"pressed": "enter"}
    assert "No keys" in tool("press_keys", keys=" + ")["error"]
    assert tool("type_text", text="hello") == {"typed_chars": 5}
    assert fake.calls == [("hotkey", ("ctrl", "s")), ("hotkey", ("winleft", "d")), ("press", "enter"), ("write", "hello")]


def test_file_tools(tmp_path):
    target = tmp_path / "sub" / "list.txt"
    assert tool("write_file", path=str(target), content="milk\n") == {"written": str(target), "bytes": 5}
    tool("write_file", path=str(target), content="eggs\n", mode="append")
    assert tool("read_file", path=str(target)) == {"path": str(target), "content": "milk\neggs\n"}
    listing = tool("read_file", path=str(tmp_path))
    assert listing["entries"] == ["sub/"]
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    assert "can't read .png" in tool("read_file", path=str(tmp_path / "pic.png"))["error"]
    assert "Not found" in tool("read_file", path=str(tmp_path / "missing.txt"))["error"]
    found = tool("find_files", name="*.TXT", folder=str(tmp_path))
    assert [m["path"] for m in found["matches"]] == [str(target)]
    assert "Folder not found" in tool("find_files", name="x", folder=str(tmp_path / "nope"))["error"]


def test_run_command(tmp_path):
    out = tool("run_command", command="echo atlas-online", working_directory=str(tmp_path))
    assert out["exit_code"] == 0 and out["stdout"].strip() == "atlas-online"
    pwd = tool("run_command", command="pwd")
    assert Path(pwd["stdout"].strip()).resolve() == Path.home().resolve()  # default folder is home, not the app dir
    assert "Folder not found" in tool("run_command", command="echo x", working_directory=str(tmp_path / "missing"))["error"]
    disabled = tool("run_command", ctx=make_ctx(allow_shell=False), command="echo hi")
    assert disabled == {"error": "Shell commands are disabled (ALLOW_SHELL=false)."}


def test_run_command_timeout(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        raise pc.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    out = tool("run_command", command="sleep 10", timeout_seconds=1)
    assert out == {"error": "Command timed out after 5s."}  # clamped to the 5 s minimum
    assert seen["timeout"] == 5 and seen["capture_output"] is True


def test_power_action_is_faked(monkeypatch):
    calls = []

    class Res:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(pc, "_run", lambda args, timeout=15: calls.append(args) or Res())
    monkeypatch.setattr(pc, "_osascript", lambda script: calls.append(script) or "")
    out = tool("power_action", action="sleep")
    assert out == {"status": "sleep initiated"} and len(calls) == 1
    assert "isn't supported" in tool("power_action", action="explode")["error"]


def test_system_snapshot_and_capabilities():
    snap = pc.system_snapshot()
    assert "os" in snap
    if "error" not in snap:
        assert {"cpu_percent", "memory", "disk", "battery", "uptime_hours"} <= set(snap)
    caps = pc.capabilities()
    assert {"platform", "python", "system_stats", "screen", "keyboard"} <= set(caps)
