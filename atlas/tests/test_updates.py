"""Shared per-user data folder + migration, the in-app updater, and cache-busted web files."""
import io
import json
import re
import zipfile

import httpx
import pytest

from app import config, updater
from conftest import run


# ----------------------------------------------------------------- per-user folder & migration
def test_user_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    assert config._user_dir() == tmp_path / "home"


def _point(monkeypatch, tmp_path):
    user, legacy = tmp_path / "user", tmp_path / "prog"
    legacy.mkdir()
    monkeypatch.setattr(config, "USER_DIR", user)
    monkeypatch.setattr(config, "ENV_PATH", user / ".env")
    monkeypatch.setattr(config, "DATA_DIR", user / "data")
    monkeypatch.setattr(config, "LEGACY_ENV_PATH", legacy / ".env")
    monkeypatch.setattr(config, "LEGACY_DATA_DIR", legacy / "data")
    return user, legacy


def test_migration_copies_old_settings_and_data(monkeypatch, tmp_path):
    user, legacy = _point(monkeypatch, tmp_path)
    (legacy / ".env").write_text("OPENROUTER_API_KEY=sk-or-v1-fake\nASSISTANT_NAME=Jarvis\n")
    (legacy / "data").mkdir()
    (legacy / "data" / "memory.json").write_text(json.dumps([{"id": "a", "fact": "Favorite color is blue"}]))
    (legacy / "data" / "browser-profile").mkdir()
    done = config.migrate_legacy_files()
    assert "settings" in done and "memory.json" in done and "browser-profile" in done
    assert config.load_env_file(user / ".env")["ASSISTANT_NAME"] == "Jarvis"
    assert json.loads((user / "data" / "memory.json").read_text())[0]["fact"] == "Favorite color is blue"
    assert (legacy / ".env").exists()  # nothing is deleted
    assert config.migrate_legacy_files() == []  # idempotent


def test_migration_merges_memories_and_keeps_current_settings(monkeypatch, tmp_path):
    user, legacy = _point(monkeypatch, tmp_path)
    (user / "data").mkdir(parents=True)
    (user / ".env").write_text("ASSISTANT_NAME=Jarvis\n")
    (legacy / ".env").write_text("ASSISTANT_NAME=Old\nHOME_CITY=Austin\n")
    (user / "data" / "memory.json").write_text(json.dumps([{"id": "1", "fact": "Sister Maya lives in Denver"}]))
    (legacy / "data").mkdir()
    (legacy / "data" / "memory.json").write_text(json.dumps([
        {"id": "2", "fact": "sister maya lives in denver"},  # duplicate (case-insensitive)
        {"id": "3", "fact": "Favorite color is blue"},
    ]))
    done = config.migrate_legacy_files()
    vals = config.load_env_file(user / ".env")
    assert vals["ASSISTANT_NAME"] == "Jarvis" and vals["HOME_CITY"] == "Austin"  # current wins, missing added
    facts = [m["fact"] for m in json.loads((user / "data" / "memory.json").read_text())]
    assert facts == ["Sister Maya lives in Denver", "Favorite color is blue"]
    assert any("memory" in d for d in done)


def test_migration_survives_corrupt_files(monkeypatch, tmp_path):
    user, legacy = _point(monkeypatch, tmp_path)
    (user / "data").mkdir(parents=True)
    (user / "data" / "memory.json").write_text("[]")
    (legacy / "data").mkdir()
    (legacy / "data" / "memory.json").write_text("{not json")
    assert config.migrate_legacy_files() == []


# ----------------------------------------------------------------- updater
def test_version_parsing_and_comparison():
    assert updater.parse_version('"""x"""\nVERSION = "2.10.1"\n') == "2.10.1"
    assert updater.parse_version("nothing here") is None
    assert updater.is_newer("2.10.0", "2.9.9") and not updater.is_newer("2.2.0", "2.2.0")
    assert not updater.is_newer(None) and not updater.is_newer("x.y", "1.0")


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


GOOD = {
    "FinGPT-branch/README.md": "repo",
    "FinGPT-branch/atlas/run.py": "print('new')",
    "FinGPT-branch/atlas/app/server.py": "# new server",
    "FinGPT-branch/atlas/app/version.py": 'VERSION = "9.0.0"\n',
    "FinGPT-branch/atlas/web/app.js": "// new",
    "FinGPT-branch/atlas/.env": "OPENROUTER_API_KEY=attacker",  # must never be copied
    "FinGPT-branch/atlas/data/memory.json": "[]",  # must never be copied
    "FinGPT-branch/atlas/../../evil.txt": "zip slip",
}


def test_install_replaces_program_files_only(tmp_path, monkeypatch):
    root = tmp_path / "atlas"
    (root / "app").mkdir(parents=True)
    (root / "run.py").write_text("print('old')")
    (root / ".env").write_text("OPENROUTER_API_KEY=mine")
    (root / "data").mkdir()
    (root / "data" / "memory.json").write_text('[{"fact": "keep me"}]')
    zp = tmp_path / "u.zip"
    zp.write_bytes(_zip(GOOD))
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    version = updater._install(zp, root)
    assert version == "9.0.0"
    assert (root / "run.py").read_text() == "print('new')" and (root / "web" / "app.js").exists()
    assert (root / ".env").read_text() == "OPENROUTER_API_KEY=mine"
    assert "keep me" in (root / "data" / "memory.json").read_text()
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()


def test_install_rejects_archives_without_atlas(tmp_path):
    zp = tmp_path / "u.zip"
    zp.write_bytes(_zip({"x/readme.md": "nope"}))
    with pytest.raises(updater.UpdateError):
        updater._install(zp, tmp_path / "atlas")


def test_check_reports_update(monkeypatch):
    def handler(req):
        assert req.url.host == "raw.githubusercontent.com"
        return httpx.Response(200, text='VERSION = "99.0.0"\n')

    monkeypatch.setattr(updater, "is_git_checkout", lambda root=None: False)
    updater._cache.update(at=0.0, result=None)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await updater.check(http, force=True)

    res = run(go())
    assert res["latest"] == "99.0.0" and res["update_available"] is True and res["can_update"] is True


def test_check_offline_is_friendly(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("down")

    updater._cache.update(at=0.0, result=None)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await updater.check(http, force=True)

    res = run(go())
    assert res["update_available"] is False and "Couldn't check" in res["error"]


def test_apply_refuses_git_checkout(monkeypatch):
    monkeypatch.setattr(updater, "is_git_checkout", lambda root=None: True)
    with pytest.raises(updater.UpdateError, match="git"):
        run(updater.apply(None))


# ----------------------------------------------------------------- server: version + cache busting
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app import server

    with TestClient(server.app, base_url="http://127.0.0.1") as c:
        yield server, c


def test_index_versions_static_urls_and_static_is_revalidated(client):
    server, c = client
    page = c.get("/").text
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert refs and all("?v=" in r for r in refs)
    assert all(server.VERSION in r for r in refs)
    r = c.get(refs[0].split("?")[0])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_state_reports_version(client):
    server, c = client
    token = re.search(r'name="atlas-token" content="([^"]+)"', c.get("/").text).group(1)
    st = c.get("/api/state", headers={"X-Atlas-Token": token}).json()
    assert st["version"] == server.VERSION and st["data_folder"]
