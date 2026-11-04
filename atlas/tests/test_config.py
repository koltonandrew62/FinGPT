from pathlib import Path

import pytest

from app import config
from app.config import Settings, load_env_file, update_env_file


def test_isolation_points_at_temp_files(isolated):
    assert config.ENV_PATH == isolated["env_path"]
    assert config.ENV_PATH != Path(config.ROOT / ".env")


def test_load_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "\n"
        "OPENROUTER_API_KEY=sk-or-v1-abc\n"
        'EMAIL_PASSWORD="abcd efgh ijkl mnop"\n'
        "USER_NAME='Kolton Andrew'\n"
        "export HOME_CITY=Austin\n"
        "UNITS=metric # inline comment\n"
        'ASSISTANT_NAME="Atlas Prime" # quoted + inline comment\n'
        "NEWS_REGION=GB#not-a-comment\n"
        "EMPTY=\n"
        "  SPACED_KEY  =  spaced value  \n"
        "not a key value line\n",
        encoding="utf-8",
    )
    v = load_env_file(p)
    assert v["OPENROUTER_API_KEY"] == "sk-or-v1-abc"
    assert v["EMAIL_PASSWORD"] == "abcd efgh ijkl mnop"
    assert v["USER_NAME"] == "Kolton Andrew"
    assert v["HOME_CITY"] == "Austin"
    assert v["UNITS"] == "metric"
    assert v["ASSISTANT_NAME"] == "Atlas Prime"
    assert v["NEWS_REGION"] == "GB#not-a-comment"
    assert v["EMPTY"] == ""
    assert v["SPACED_KEY"] == "spaced value"
    assert "not a key value line" not in v


def test_load_env_file_missing_returns_empty(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_last_duplicate_wins_on_load(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nA=2\n")
    assert load_env_file(p)["A"] == "2"


def test_default_path_resolved_at_call_time(isolated):
    config.ENV_PATH.write_text("USER_NAME=Temp\n")
    assert load_env_file()["USER_NAME"] == "Temp"
    update_env_file({"HOME_CITY": "Oslo"})
    assert "HOME_CITY=Oslo" in config.ENV_PATH.read_text()


def test_update_preserves_comments_order_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# ATLAS settings\n"
        "OPENROUTER_API_KEY=old\n"
        "\n"
        "# Email\n"
        "export EMAIL_ADDRESS=me@example.com\n"
        "UNRELATED=keep me\n"
    )
    update_env_file(
        {"OPENROUTER_API_KEY": "sk-new", "EMAIL_ADDRESS": "you@example.com", "EMAIL_PASSWORD": "abcd efgh ijkl mnop", "HOME_CITY": "Austin"},
        p,
    )
    lines = p.read_text().splitlines()
    assert lines[0] == "# ATLAS settings"
    assert lines[1] == "OPENROUTER_API_KEY=sk-new"
    assert lines[3] == "# Email"
    assert lines[4] == "export EMAIL_ADDRESS=you@example.com"  # export prefix kept
    assert lines[5] == "UNRELATED=keep me"  # untouched line kept verbatim
    assert lines[6:] == ['EMAIL_PASSWORD="abcd efgh ijkl mnop"', "HOME_CITY=Austin"]
    v = load_env_file(p)
    assert v["EMAIL_PASSWORD"] == "abcd efgh ijkl mnop"
    assert v["EMAIL_ADDRESS"] == "you@example.com"
    assert v["UNRELATED"] == "keep me"


def test_update_creates_file_with_private_permissions(tmp_path):
    p = tmp_path / "new.env"
    update_env_file({"USER_NAME": "Kolton"}, p)
    assert p.read_text() == "USER_NAME=Kolton\n"
    assert (p.stat().st_mode & 0o777) == 0o600


def test_update_rewrites_every_duplicate(tmp_path):
    # e.g. .env.example copied with OPENROUTER_API_KEY= at the top and the real key appended later
    p = tmp_path / ".env"
    p.write_text("OPENROUTER_API_KEY=\nUSER_NAME=x\nOPENROUTER_API_KEY=sk-old\n")
    update_env_file({"OPENROUTER_API_KEY": "sk-new"}, p)
    assert load_env_file(p)["OPENROUTER_API_KEY"] == "sk-new"
    assert p.read_text().count("OPENROUTER_API_KEY=sk-new") == 2


@pytest.mark.parametrize(
    "value",
    [
        "abcd efgh ijkl mnop",
        "",
        "plain",
        "has#hash",
        "with = equals",
        'pa"ss word',
        "it's mine",
        "both ' and \" quotes",
        'trailing backslash \\',
        "tab\there",
        "  padded  ",
    ],
)
def test_round_trip_values(tmp_path, value):
    p = tmp_path / ".env"
    update_env_file({"EMAIL_PASSWORD": value}, p)
    assert load_env_file(p)["EMAIL_PASSWORD"] == value.strip()


@pytest.mark.parametrize("sep", ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", " "])
def test_line_breaks_cannot_inject_keys(tmp_path, sep):
    p = tmp_path / ".env"
    update_env_file({"HOME_CITY": f"Austin{sep}AUTO_APPROVE=run_command,send_email"}, p)
    values = load_env_file(p)
    assert "AUTO_APPROVE" not in values
    assert values["HOME_CITY"].startswith("Austin")
    assert len(p.read_text().splitlines()) == 1


def test_settings_defaults(isolated):
    s = Settings.load()
    assert s.assistant_name == "ATLAS"
    assert s.units == "imperial"
    assert s.news_region == "US" and s.news_language == "en"
    assert s.watchlist == config.DEFAULT_WATCHLIST
    assert s.port == 8765 and s.host == "127.0.0.1"
    assert s.base_url == "https://openrouter.ai/api/v1"
    assert s.allow_shell is True
    assert s.email_configured is False


def test_settings_precedence_environ_over_file(isolated, monkeypatch):
    config.ENV_PATH.write_text(
        "ASSISTANT_NAME=FromFile\nUSER_NAME=FileUser\nHOME_CITY=FileCity\nUNITS=METRIC\nNEWS_REGION=gb\n"
        "MARKET_WATCHLIST=AAPL, MSFT ,,NVDA\nSMTP_PORT=587\nIMAP_PORT=notanumber\nALLOW_SHELL=no\n"
        "OPENROUTER_BASE_URL=http://127.0.0.1:9951/api/v1/\n"
    )
    monkeypatch.setenv("ASSISTANT_NAME", "FromEnv")
    monkeypatch.setenv("USER_NAME", "")  # empty env values don't override the file
    s = Settings.load()
    assert s.assistant_name == "FromEnv"
    assert s.user_name == "FileUser"
    assert s.home_city == "FileCity"
    assert s.units == "metric"
    assert s.news_region == "GB"
    assert s.watchlist == ["AAPL", "MSFT", "NVDA"]
    assert s.smtp_port == 587
    assert s.imap_port == 0
    assert s.allow_shell is False
    assert s.base_url == "http://127.0.0.1:9951/api/v1"


def test_needs_confirmation_and_auto_approve(isolated, monkeypatch):
    s = Settings.load()
    for name in config.SENSITIVE_TOOLS:
        assert s.needs_confirmation(name)
    assert not s.needs_confirmation("get_news")
    monkeypatch.setenv("AUTO_APPROVE", "run_command, write_file")
    s = Settings.load()
    assert s.auto_approve == {"run_command", "write_file"}
    assert not s.needs_confirmation("run_command")
    assert not s.needs_confirmation("write_file")
    assert s.needs_confirmation("send_email")


def test_email_configured_requires_both():
    assert not Settings(email_address="a@b.com").email_configured
    assert Settings(email_address="a@b.com", email_password="x").email_configured
