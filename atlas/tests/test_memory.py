import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.tools import call_tool, memory
from conftest import make_ctx, run


def tool(_tool_name, **args):
    return json.loads(run(call_tool(_tool_name, args, make_ctx())))


def test_data_dir_is_redirected(isolated):
    tool("remember", fact="Likes tea")
    assert (isolated["data_dir"] / "memory.json").exists()


# --------------------------------------------------------------------- memory
def test_remember_dedupes_case_insensitively():
    assert tool("remember", fact="Prefers metric units")["status"] == "remembered"
    assert tool("remember", fact="  prefers METRIC units ")["status"] == "already known"
    assert [m["fact"] for m in memory.memories()] == ["Prefers metric units"]
    m = memory.memories()[0]
    assert len(m["id"]) == 8 and m["saved"]


def test_remember_rejects_empty():
    assert "error" in tool("remember", fact="   ")
    assert memory.memories() == []


def test_forget_by_substring():
    for f in ["Sister is named Maya", "Maya likes jazz", "Works at Acme"]:
        tool("remember", fact=f)
    assert tool("forget", match="maya") == {"removed": 2}
    assert [m["fact"] for m in memory.memories()] == ["Works at Acme"]
    assert tool("forget", match="nothing like this") == {"removed": 0}


@pytest.mark.parametrize("match", ["", "   "])
def test_forget_empty_match_does_not_wipe_memory(match):
    tool("remember", fact="Sister is named Maya")
    assert "error" in tool("forget", match=match)
    assert len(memory.memories()) == 1


def test_delete_memory_by_id():
    tool("remember", fact="A")
    tool("remember", fact="B")
    mid = memory.memories()[0]["id"]
    assert memory.delete_memory(mid) is True
    assert memory.delete_memory(mid) is False
    assert [m["fact"] for m in memory.memories()] == ["B"]


def test_memory_is_capped():
    memory.save("memory", [{"id": str(i), "fact": f"f{i}", "saved": ""} for i in range(200)])
    tool("remember", fact="newest")
    items = memory.memories()
    assert len(items) == 200 and items[-1]["fact"] == "newest" and items[0]["fact"] == "f1"


def test_corrupt_file_falls_back_to_default(isolated):
    memory._path("memory").write_text("{not json")
    assert memory.memories() == []


# ------------------------------------------------------------------- contacts
def seed_contacts():
    tool("save_contact", name="Mom", email="mom@example.com", aliases=["mother", "ma"])
    tool("save_contact", name="Maya Patel", email="maya@example.com", aliases=["sis"])
    tool("save_contact", name="Maya Chen", email="mchen@example.com")
    tool("save_contact", name="Coach Rivera", email="rivera@example.com", aliases="coach, rivera")
    tool("save_contact", name="Sisko Ben", email="ben@example.com")


def test_find_contact_exact_alias_and_partial():
    seed_contacts()
    assert memory.find_contact("mom")["email"] == "mom@example.com"
    assert memory.find_contact("  MOTHER ")["email"] == "mom@example.com"
    assert memory.find_contact("Maya Patel")["email"] == "maya@example.com"
    assert memory.find_contact("rivera")["email"] == "rivera@example.com"
    assert memory.find_contact("chen")["email"] == "mchen@example.com"  # unique partial name match
    # exact alias wins over a partial name match on another contact ("Sisko")
    assert memory.find_contact("sis")["email"] == "maya@example.com"


def test_find_contact_ambiguous_or_unknown():
    seed_contacts()
    assert memory.find_contact("maya") is None  # two Mayas
    assert memory.find_contact("nobody") is None
    assert memory.find_contact("") is None


def test_save_contact_updates_and_validates():
    tool("save_contact", name="Mom", email="old@example.com")
    out = tool("save_contact", name="mom", email="new@example.com", phone="555")
    assert out == {"status": "saved", "name": "mom", "email": "new@example.com"}
    assert len(memory.contacts()) == 1 and memory.contacts()[0]["phone"] == "555"
    assert "error" in tool("save_contact", name="X", email="not-an-email")
    tool("save_contact", name="Coach", email="c@example.com", aliases="coach r, rivera")
    assert memory.find_contact("Coach")["aliases"] == ["coach r", "rivera"]


def test_list_contacts_filter():
    seed_contacts()
    assert len(tool("list_contacts")["contacts"]) == 5
    assert [c["name"] for c in tool("list_contacts", query="maya")["contacts"]] == ["Maya Chen", "Maya Patel"]


# ---------------------------------------------------------------------- notes
def test_notes_add_search_delete():
    n1 = tool("add_note", text="Buy milk and eggs")["note"]
    tool("add_note", text="Project ideas: voice HUD", title="Ideas")
    assert n1["title"] == "Buy milk and eggs"
    found = tool("search_notes", query="IDEAS")["notes"]
    assert [n["title"] for n in found] == ["Ideas"]
    recent = tool("search_notes")["notes"]
    assert [n["title"] for n in recent] == ["Ideas", "Buy milk and eggs"]  # newest first
    assert len(tool("search_notes", limit=1)["notes"]) == 1
    assert tool("delete_note", id=n1["id"]) == {"removed": 1}
    assert tool("delete_note", id=n1["id"]) == {"removed": 0}


# ------------------------------------------------------------------ reminders
def test_set_reminder_minutes_and_list_sorted():
    later = tool("set_reminder", message="later", minutes_from_now=60)
    sooner = tool("set_reminder", message="sooner", minutes_from_now=5)
    assert later["status"] == "scheduled" and later["due_human"]
    due = datetime.fromisoformat(sooner["reminder"]["due"])
    assert due.tzinfo is not None
    assert timedelta(minutes=4) < due - datetime.now().astimezone() <= timedelta(minutes=5)
    assert [r["message"] for r in tool("list_reminders")["reminders"]] == ["sooner", "later"]


def test_set_reminder_iso_without_tz_is_local():
    at = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
    out = tool("set_reminder", message="dentist", at=at.isoformat(timespec="minutes"))
    due = datetime.fromisoformat(out["reminder"]["due"])
    assert due.tzinfo is not None
    assert due.replace(tzinfo=None) == at.replace(second=0)


def test_set_reminder_iso_with_z():
    at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    out = tool("set_reminder", message="call", at=at.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert datetime.fromisoformat(out["reminder"]["due"]) == at


@pytest.mark.parametrize(
    "args, message",
    [
        ({"at": "2001-01-01T09:00"}, "in the past"),
        ({"minutes_from_now": -30}, "in the past"),
        ({"at": "next tuesday"}, "ISO format"),
        ({}, "Give either"),
    ],
)
def test_set_reminder_errors(args, message):
    out = tool("set_reminder", message="x", **args)
    assert message in out["error"]
    assert memory.pending_reminders() == []


def test_cancel_reminder():
    r = tool("set_reminder", message="x", minutes_from_now=10)["reminder"]
    assert tool("cancel_reminder", id=r["id"]) == {"removed": 1}
    assert tool("cancel_reminder", id=r["id"]) == {"removed": 0}
    assert memory.pending_reminders() == []


def test_pop_due_reminders_fires_once():
    now = datetime.now().astimezone()
    memory.save(
        "reminders",
        [
            {"id": "past", "message": "stretch", "due": (now - timedelta(minutes=1)).isoformat(timespec="seconds"), "fired": False},
            {"id": "naive", "message": "naive past", "due": (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds"), "fired": False},
            {"id": "future", "message": "later", "due": (now + timedelta(hours=1)).isoformat(timespec="seconds"), "fired": False},
            {"id": "bad", "message": "garbage", "due": "not a date", "fired": False},
            {"id": "old", "message": "fired long ago", "due": (now - timedelta(days=3)).isoformat(timespec="seconds"), "fired": True},
        ],
    )
    due = memory.pop_due_reminders()
    assert sorted(r["id"] for r in due) == ["naive", "past"]
    assert all(r["fired"] for r in due)
    assert memory.pop_due_reminders() == []
    remaining = {r["id"] for r in memory.load("reminders", [])}
    assert remaining == {"past", "naive", "future", "bad"}  # "old" pruned, just-fired kept for a day
    assert [r["id"] for r in memory.pending_reminders()] == ["future", "bad"]


def test_pending_reminders_sort_by_instant_not_text():
    memory.save(
        "reminders",
        [
            {"id": "a", "message": "13:00 UTC", "due": "2030-01-01T08:00:00-05:00", "fired": False},
            {"id": "b", "message": "09:00 UTC", "due": "2030-01-01T09:00:00+00:00", "fired": False},
        ],
    )
    assert [r["id"] for r in memory.pending_reminders()] == ["b", "a"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_naive_reminder_time_uses_that_dates_dst_offset():
    original = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        if datetime(2027, 7, 1).astimezone().utcoffset() == datetime(2027, 1, 1).astimezone().utcoffset():
            pytest.skip("tz database not available")
        summer = tool("set_reminder", message="summer", at="2027-07-01T09:00")["reminder"]["due"]
        winter = tool("set_reminder", message="winter", at="2027-01-15T09:00")["reminder"]["due"]
        assert summer.endswith("-04:00")
        assert winter.endswith("-05:00")
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
