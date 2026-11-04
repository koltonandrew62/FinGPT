"""Tests for atlas/app/tools/browser.py.

The browser tests drive a real (headless) Chromium through a fake restaurant site served from a local
http.server; nothing touches the internet. They skip cleanly when Playwright/Chromium can't launch.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.llm import LLMError
from app.tools import TOOLS, ToolContext, ToolError, call_tool
from app.tools import browser


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- fake restaurant site
def _page(title: str, body: str) -> bytes:
    return f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head><body>{body}</body></html>".encode()


ATTACK_PAGE = """
  <p>Trattoria Uno · Party of 2 · 7:00 PM</p>
  <form action="/complete" method="post" id="reservation-form">
    <input id="em2" name="email" aria-label="Email">
    <label for="pw2" tabindex="0">Your password</label><input id="pw2" name="pw" type="password">
    <input id="showpw" name="password" type="text" autocomplete="current-password" aria-label="Password (shown)" value="hunter2">
    <input id="pw3" name="newPassword" type="text" aria-label="Choose one" value="s3cret!">
    <input type="submit" value="Complete reservation">
  </form>
  <div role="textbox" tabindex="0" aria-label="Notes" style="min-height:20px"
       onkeydown="window.__keys = (window.__keys || '') + (event.key.length === 1 ? event.key : '[' + event.key + ']');
                  if (event.key === 'Enter') document.title = 'enter pressed'"></div>
  <div role="textbox" tabindex="0" aria-label="Sneaky" style="min-height:20px"
       onclick="document.getElementById('pw2').focus()"></div>
  <a href="/">Home page</a>"""

FORMLESS_PAGE = """
  <div class="promo"><input id="promo" aria-label="Promo code"><button onclick="document.title = 'ordered'">Place order</button></div>
  <div class="compose"><div contenteditable="true" aria-label="Message body" style="min-height:20px;border:1px solid"></div></div>
  <div class="find"><input aria-label="Search dishes"><button>Search</button></div>
  <p><button id="later" onclick="document.title = 'clicked later'">View menu</button></p>
  <div role="checkbox" aria-checked="false" tabindex="0" aria-label="Subscribe to offers"
       onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') !== 'true')">Offers</div>
  <p><input aria-label="Reference" id="refno"></p>
  <p><label for="hiddenbuy" tabindex="0">Continue</label>
     <button id="hiddenbuy" style="position:absolute;left:-9999px" onclick="document.title = 'bought'">Buy now</button></p>"""


class FakeSite(BaseHTTPRequestHandler):
    port = 0
    posts: list = []  # every POST path, so tests can prove nothing was submitted

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, content: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if path == "/":
            body = """
              <h1>TableFinder</h1>
              <form action="/search" method="get" id="search-form">
                <label for="q">Restaurant or cuisine</label><input id="q" name="q" placeholder="Restaurant or cuisine">
                <label for="party">Party size</label>
                <select id="party" name="party">
                  <option value="1">1 person</option><option value="2" selected>2 people</option><option value="4">4 people</option>
                </select>
                <button type="submit">Let's go</button>
              </form>
              <p><a href="/tall">Tall page</a> <a href="/frames">Widget page</a> <a href="/popup">Popup page</a></p>"""
            return self._send(_page("TableFinder", body))
        if path == "/search":
            term = html.escape(q.get("q", ""))
            party = html.escape(q.get("party", "2"))
            body = f"""<h1>Results for {term}</h1>
              <ul><li><a href="/restaurant/1?party={party}">Trattoria Uno</a> · Italian · $$</li>
                  <li><a href="/restaurant/2?party={party}">Sushi Dos</a> · Japanese · $$$</li></ul>"""
            return self._send(_page(f"Results for {term}", body))
        if path.startswith("/restaurant/"):
            party = html.escape(q.get("party", "2"))
            slots = "".join(f'<button name="time" value="{t}">{t}</button>' for t in ("6:30 PM", "7:00 PM", "7:30 PM"))
            body = f"""<h1>Trattoria Uno</h1><p>Party of {party} · Saturday, November 7</p>
              <form action="/details" method="get" id="slot-form"><input type="hidden" name="party" value="{party}">{slots}</form>
              <p>No online booking for large groups? Call <a href="tel:+15550100199">(555) 010-0199</a></p>"""
            return self._send(_page("Trattoria Uno - Select a time", body))
        if path == "/details":
            t = html.escape(q.get("time", "7:00 PM"))
            party = html.escape(q.get("party", "2"))
            body = f"""<h1>You're almost done!</h1>
              <p>Trattoria Uno · Saturday, November 7 · {t} · Party of {party}</p>
              <form action="/complete" method="post" id="reservation-form">
                <label for="fn">First name</label><input id="fn" name="first_name" required>
                <label for="ln">Last name</label><input id="ln" name="last_name">
                <label for="ph">Phone number</label><input id="ph" name="phone" type="tel">
                <label for="em">Email</label><input id="em" name="email" type="email">
                <label for="pw">Create a password (optional)</label><input id="pw" name="password" type="password">
                <label for="cc">Card number to hold the table</label><input id="cc" name="cardnumber" autocomplete="cc-number">
                <label for="cvv">CVV</label><input id="cvv" name="cvv">
                <label><input type="checkbox" id="sms" name="sms" style="position:absolute;opacity:0;width:1px;height:1px"> Text me updates</label>
                <input type="hidden" name="time" value="{t}"><input type="hidden" name="party" value="{party}">
                <button type="submit">Complete reservation</button>
              </form>"""
            return self._send(_page("Trattoria Uno - Complete your reservation", body))
        if path == "/tall":
            return self._send(_page("Tall", '<div style="height:3000px">Top of a long page</div><button id="deep">Deep button</button>'))
        if path == "/many":
            links = "".join(
                f'<p><a href="/r/{i}">Restaurant number {i} with a rather long descriptive name for testing</a></p>' for i in range(400)
            )
            return self._send(_page("Many", links))
        if path == "/frames":
            src = f"http://localhost:{self.port}/widget"
            body = f'<h1>Trattoria Uno</h1><iframe src="{src}" style="width:500px;height:300px;border:1px solid #ccc"></iframe>'
            return self._send(_page("Widget host", body))
        if path == "/widget":
            body = """<p>Trattoria Uno booking widget · Party of 2 · 8:00 PM</p>
              <button onclick="document.body.insertAdjacentHTML('beforeend', '<p>Booked via widget</p>')">Reserve now</button>"""
            return self._send(_page("Widget", body))
        if path == "/popup":
            return self._send(_page("Popup", '<a href="/search?q=popup" target="_blank">Open results in new tab</a>'))
        if path == "/dialog":
            return self._send(_page("Dialog", """<button onclick="document.title = confirm('Really start over?') ? 'yes' : 'no'">Ask me</button>"""))
        if path == "/widgets":
            body = """
              <div id="host"></div>
              <script>
                const root = document.getElementById('host').attachShadow({mode: 'open'});
                root.innerHTML = '<button id="sb">Book this table</button>';
                root.getElementById('sb').addEventListener('click', () => { document.title = 'shadow clicked'; });
              </script>
              <div class="slot" style="cursor:pointer;display:inline-block;padding:4px" onmousedown="">8:15 PM</div>
              <div role="grid"><div role="row">
                <div role="gridcell" aria-selected="true" tabindex="0">14</div><div role="gridcell" tabindex="-1">15</div>
              </div></div>
              <button style="display:none">Hidden thing</button>
              <div onclick="void 0" style="width:100vw;height:100vh;position:absolute;top:0;left:0;z-index:-1"></div>
              <div contenteditable="true" aria-label="Special requests" style="min-height:20px;border:1px solid"></div>"""
            return self._send(_page("Widgets", body))
        if path == "/attack":
            return self._send(_page("Attack", ATTACK_PAGE))
        if path == "/formless":
            return self._send(_page("Formless", FORMLESS_PAGE))
        if path == "/modal":
            nav = "".join(f'<a href="/n/{i}">Neighbourhood guide number {i}</a> ' for i in range(80))
            body = f"""<header>{nav}</header><main><h1>Carbone</h1><p>{'Italian-American classics. ' * 20}</p></main>
              <div role="dialog" aria-modal="true"><h2>Complete your booking</h2>
                <p>Carbone · Fri, Nov 6 · 8:30 PM · 4 guests</p><button>Reserve Now</button></div>"""
            return self._send(_page("Carbone", body))
        if path == "/go-home":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.port}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/chat":
            body = '<div class="composer"><textarea id="msg" aria-label="Message"></textarea><button aria-label="Send">&gt;</button></div>'
            return self._send(_page("Chat", body))
        return self._send(_page("Not found", "<p>404</p>"), 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        FakeSite.posts.append(urlparse(self.path).path)
        if urlparse(self.path).path == "/complete":
            body = (
                f"<h1>Reservation confirmed!</h1><p>Confirmation #TU-4821 for {html.escape(form.get('first_name', ''))}, "
                f"party of {html.escape(form.get('party', ''))} at {html.escape(form.get('time', ''))}.</p>"
                f"<p>Card on file: {'yes' if form.get('cardnumber') else 'no'}</p>"
            )
            return self._send(_page("Confirmed", body))
        return self._send(_page("Not found", "<p>404</p>"), 404)


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
    FakeSite.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{FakeSite.port}"
    server.shutdown()


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def resolve_model(self):
        return "test/vision-model"

    async def complete(self, messages, model, **extra):
        self.calls.append({"messages": messages, "model": model, **extra})
        return {"choices": [{"message": {"content": "The Complete reservation button is tagged e9."}}]}


@pytest.fixture
def ctx():
    def handler(request):  # nothing in these tests should use HTTP, and the internet is off-limits anyway
        return httpx.Response(500, text="no network in tests")

    return ToolContext(settings=Settings(home_city="Boston"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), llm=FakeLLM())


@pytest.fixture(scope="module")
def live(site, tmp_path_factory):
    """A real headless browser for the module, or skip."""
    mp = pytest.MonkeyPatch()
    # Patch the settings reader rather than os.environ: the shared conftest clears settings env vars per test,
    # and the recovery test relaunches the browser mid-module.
    overrides = {"BROWSER_HEADLESS": "true"}
    if os.path.exists("/opt/pw-browsers/chromium") and not os.environ.get("BROWSER_EXECUTABLE"):
        overrides["BROWSER_EXECUTABLE"] = "/opt/pw-browsers/chromium"  # preinstalled build in this container
    real_setting = browser._setting
    mp.setattr(browser, "_setting", lambda key, default="": overrides.get(key) or real_setting(key, default))
    mp.setattr(browser, "PROFILE_DIR", tmp_path_factory.mktemp("browser-profile"))
    mp.setattr(browser, "ALLOW_PRIVATE", True)
    try:
        run(browser._call(browser._page))
    except Exception as exc:
        mp.undo()
        pytest.skip(f"Chromium can't launch here: {exc}")
    yield site
    try:
        run(browser.shutdown())
        assert browser._S.pw is None and browser._S.context is None
    finally:
        mp.undo()


def ref_of(snap: dict, label: str, **match) -> str:
    for el in snap["elements"]:
        if label.lower() in (el.get("label") or "").lower() and all(el.get(k) == v for k, v in match.items()):
            return el["ref"]
    raise AssertionError(f"No element labelled {label!r} in {json.dumps(snap['elements'])[:2000]}")


def element(snap: dict, ref: str) -> dict:
    return next(e for e in snap["elements"] if e["ref"] == ref)


# ----------------------------------------------------------------------------- pure logic (no browser)
def test_registered_tools_and_gates():
    for name in ("browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_fill", "browser_select",
                 "browser_press", "browser_scroll", "browser_back", "browser_look", "browser_close", "reservation_search"):
        assert name in TOOLS, name
    assert TOOLS["browser_click"].gate is browser._click_gate
    assert TOOLS["browser_type"].gate is browser._type_gate
    assert TOOLS["browser_press"].gate is browser._press_gate
    assert TOOLS["browser_open"].gate is None


def test_url_guard(monkeypatch):
    monkeypatch.setattr(browser, "ALLOW_PRIVATE", False)
    for bad in ("http://127.0.0.1:8000/", "http://localhost/", "localhost:3000", "http://192.168.1.10/admin",
                "file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/x", "data:text/html,hi", ""):
        with pytest.raises(ToolError):
            browser._check_url(bad)
    assert browser._check_url("opentable.com") == "https://opentable.com"
    assert browser._check_url("https://www.yelp.com/search?find_desc=x") == "https://www.yelp.com/search?find_desc=x"
    monkeypatch.setattr(browser, "ALLOW_PRIVATE", True)
    assert browser._check_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/"


def test_intent_classification():
    def btn(label, **kw):
        return {"tag": "button", "type": "", "label": label, "text": kw.pop("text", label), **kw}

    for label in ("Book now", "Reserve", "Complete reservation", "Confirm & pay", "Place order", "Pay with PayPal",
                  "Buy now", "Checkout", "Check-out", "Submit", "Send", "Sign up", "Subscribe", "Delete account",
                  "Cancel reservation", "Transfer", "Donate"):
        assert browser._click_intent(btn(label)), label
    for label in ("7:00 PM", "Books", "Cancel", "Sign in", "Add to cart", "Reservations", "Facebook", "Resend code", "Next"):
        assert browser._click_intent(btn(label)) is None, label
    # A time-slot button stays ungated even if its aria-label says "Reserve".
    assert browser._click_intent(btn("Reserve 7:00 PM", aria="Reserve 7:00 PM", text="7:00 PM")) is None
    # Generic submit buttons are judged by their form.
    assert browser._click_intent(btn("Continue", is_submit=True, form={"action": "/checkout/pay"}))
    assert browser._click_intent(btn("Continue", is_submit=True, form={"id": "reservation-form"}))
    assert browser._click_intent(btn("Search", is_submit=True, form={"action": "/search"})) is None
    assert browser._click_intent(btn("Find a table", is_submit=True, form={"id": "reservation-form"})) is None
    # Typing targets and checkboxes are never click-gated.
    assert browser._click_intent({"tag": "input", "type": "email", "label": "Send receipts to"}) is None
    assert browser._click_intent({"tag": "input", "type": "checkbox", "label": "Subscribe to news"}) is None
    assert browser._click_intent({"tag": "input", "type": "submit", "label": "", "text": "", "value": "Book table"})
    # Enter in a field submits its form's default button.
    assert browser._enter_intent({"tag": "input", "type": "email", "form": {"default_submit": "Complete reservation"}})
    assert browser._enter_intent({"tag": "input", "type": "text", "form": {"default_submit": "Let's go", "action": "/search"}}) is None
    assert browser._enter_intent({"tag": "textarea", "type": "", "nearby_buttons": ["Attach", "Send"]})
    assert browser._enter_intent({"tag": "input", "type": "text"}) is None
    # More final-button wordings, including common non-English ones.
    for label in ("Order now", "Request reservation", "Request to book", "Join waitlist", "Finish booking", "Reservar",
                  "Réserver", "Jetzt kaufen", "Zahlungspflichtig bestellen", "Valider ma réservation", "Prenota",
                  "Bevestigen", "Comprar"):
        assert browser._click_intent(btn(label)), label
    for label in ("Ordina per prezzo", "Reservations", "Menu", "2 people", "Show more"):
        assert browser._click_intent(btn(label)) is None, label
    # Only a real time is exempt: a bare number with a final aria-label is still gated.
    assert browser._click_intent(btn("Delete item 2", aria="Delete item 2", text="2"))
    assert browser._click_intent(btn("Reserve at 8 pm", aria="Reserve at 8 pm", text="8 pm")) is None
    # A final verb in the form beats harmless-looking words in its query string.
    assert browser._click_intent(btn("Continue", is_submit=True, form={"action": "/checkout/pay?query=x"}))
    # Clicking a label activates its control.
    assert browser._click_intent({"tag": "label", "label": "Next", "text": "Next",
                                  "control": btn("Place order", tag="button")})
    # Ctrl/Cmd+Enter sends from text boxes; Enter in a form-less rich-text box usually sends.
    assert browser._enter_intent({"tag": "textarea"}, "Control+Enter")
    assert browser._enter_intent({"tag": "div", "editable": True}, "Enter")
    assert browser._enter_intent({"tag": "div", "role": "textbox", "nearby_buttons": ["Send"]})
    assert browser._press_reason("Meta+Enter", {"tag": "div", "editable": True})
    assert browser._press_reason("Tab", {"tag": "div", "editable": True}) is None


def test_sensitive_classification():
    assert browser._sensitive_kind({"type": "password"}) == "password"
    assert browser._sensitive_kind({"autocomplete": "cc-number"}) == "payment card"
    assert browser._sensitive_kind({"autocomplete": "billing cc-exp"}) == "payment card"
    for name in ("cardnumber", "card_number", "cvc", "cvv2", "ccnum", "cardholder-name", "iban"):
        assert browser._sensitive_kind({"name": name}) == "payment card", name
    assert browser._sensitive_kind({"labels": "Card number"}) == "payment card"
    assert browser._sensitive_kind({"url": "https://js.stripe.com/v3/elements-inner"}) == "payment card"
    for name in ("email", "phone", "discard", "postcard", "scorecard", "first_name", "special_requests"):
        assert browser._sensitive_kind({"name": name}) is None, name
    # Password fields shown as plain text ("show password"), and labels that stand for a secret field.
    assert browser._sensitive_kind({"tag": "input", "type": "text", "autocomplete": "current-password"}) == "password"
    for name in ("password", "newPassword", "user_pass", "pwd", "passcode"):
        assert browser._sensitive_kind({"tag": "input", "type": "text", "name": name}) == "password", name
    for name in ("passenger_count", "passport_country", "compass"):
        assert browser._sensitive_kind({"tag": "input", "type": "text", "name": name}) is None, name
    assert browser._sensitive_kind({"tag": "label", "control": {"tag": "input", "type": "password"}}) == "password"
    # A "save my card" checkbox or a "Pay" button holds no secret.
    assert browser._sensitive_kind({"tag": "input", "type": "checkbox", "name": "save_card"}) is None
    assert browser._sensitive_kind({"tag": "button", "label": "Pay with card"}) is None
    assert browser._as_bool("false") is False and browser._as_bool("true") is True and browser._as_bool(None, True) is True
    assert browser._looks_like_card_number("4111 1111 1111 1111")
    assert browser._looks_like_card_number("3782-822463-10005")
    assert not browser._looks_like_card_number("+1 (415) 555-0134")
    assert not browser._looks_like_card_number("Party of 2 at 7pm")


def test_captcha_detection_is_narrow():
    assert browser._blocker("Just a moment...", [""], [])
    assert browser._blocker("Shop", ["Please verify you are human"], [])
    assert browser._blocker("Login", ["I'm not a robot"], [])
    assert browser._blocker("Shop", [""], ["https://www.google.com/recaptcha/api2/bframe?hl=en&k=abc"])
    # An invisible reCAPTCHA badge / footer on a normal booking form is not a blocker.
    assert browser._blocker(
        "Complete your reservation", ["This site is protected by reCAPTCHA and the Google Privacy Policy"],
        ["https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=invisible"],
    ) is None


def test_key_normalisation():
    assert browser._norm_key("enter") == "Enter"
    assert browser._norm_key("esc") == "Escape"
    assert browser._norm_key("ctrl+a") == "Control+a"
    assert browser._norm_key("Shift + Tab") == "Shift+Tab"
    assert browser._norm_key("down") == "ArrowDown"
    assert browser._norm_key("f5") == "F5"
    with pytest.raises(ToolError):
        browser._norm_key("")
    with pytest.raises(ToolError):
        browser._norm_key("hyper+x")


def test_reservation_links_and_parsing(ctx):
    when = date.today() + timedelta(days=3)
    out = run(browser.reservation_search(ctx, query="sushi", city="Boston, MA", date=when.isoformat(), time="7:30 PM",
                                         party_size=4, open=False))
    links = out["links"]
    assert links["opentable"] == (
        f"https://www.opentable.com/s?dateTime={when.isoformat()}T19%3A30%3A00&covers=4&term=sushi+Boston%2C+MA"
    )
    assert links["google_maps"] == "https://www.google.com/maps/search/?api=1&query=sushi+Boston%2C+MA"
    assert links["yelp"] == "https://www.yelp.com/search?find_desc=sushi&find_loc=Boston%2C+MA"
    assert links["resy"] == "https://resy.com/"
    assert out["search"] == {"query": "sushi", "city": "Boston, MA", "date": when.isoformat(),
                             "weekday": when.strftime("%A"), "time": "19:30", "party_size": 4}
    assert "snapshot" not in out
    # Defaults: home city from settings, 19:00, party of 2.
    out = run(browser.reservation_search(ctx, query="Trattoria Uno", open=False))
    assert "covers=2" in out["links"]["opentable"] and "T19%3A00%3A00" in out["links"]["opentable"]
    assert "term=Trattoria+Uno+Boston" in out["links"]["opentable"]
    assert browser._parse_time("19:00") == (19, 0)
    assert browser._parse_time("7pm") == (19, 0)
    assert browser._parse_time("12:15 am") == (0, 15)
    assert browser._parse_date("tomorrow") == date.today() + timedelta(days=1)
    for bad in ({"date": "2001-01-01"}, {"date": "next tuesdayish"}, {"time": "25:00"}, {"party_size": 0}, {"party_size": 50}):
        with pytest.raises(ToolError):
            run(browser.reservation_search(ctx, query="x", open=False, **bad))


def test_missing_playwright_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(ToolError) as err:
        browser._pw_api()
    assert "pip install playwright" in str(err.value) and "playwright install chromium" in str(err.value)


def test_actions_before_launch_report_not_open(ctx, monkeypatch):
    monkeypatch.setattr(browser, "_S", browser._State())
    with pytest.raises(ToolError) as err:
        run(browser.browser_click(ctx, ref="e1"))
    assert "isn't open" in str(err.value)
    assert run(browser._click_gate(ctx, {"ref": "e1"})) is None
    assert run(browser._press_gate(ctx, {"key": "Enter"})) is None
    assert json.loads(run(call_tool("browser_snapshot", {}, ctx)))["error"].startswith("ATLAS's browser isn't open")


# ----------------------------------------------------------------------------- real browser
def test_launch_failure_explains_install(live, monkeypatch):
    fresh = browser._State()
    fresh.pw = browser._S.pw  # share the running driver; only the launch attempts change
    monkeypatch.setattr(browser, "_S", fresh)
    monkeypatch.setattr(browser, "_launch_attempts", lambda: [("bogus", {"executable_path": "/nonexistent/chrome"})])
    with pytest.raises(ToolError) as err:
        run(browser._call(browser._launch))
    assert "playwright install chromium" in str(err.value)


def test_full_booking_flow(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/"))
    assert snap["title"] == "TableFinder" and snap["status"].startswith("Opened 127.0.0.1")
    assert "untrusted" in snap["note"]
    q = ref_of(snap, "Restaurant or cuisine", tag="input")
    party = ref_of(snap, "Party size", tag="select")
    assert element(snap, party)["options"] == ["1 person", "2 people", "4 people"]
    assert element(snap, party)["value"] == "2 people"

    snap = run(browser.browser_select(ctx, ref=party, option="4 people"))
    assert element(snap, party)["value"] == "4 people"

    # Enter in the search box: harmless, so no approval card.
    assert run(browser._type_gate(ctx, {"ref": q, "text": "italian", "submit": True})) is None
    snap = run(browser.browser_type(ctx, ref=q, text="italian", submit=True))
    assert snap["title"] == "Results for italian"

    snap = run(browser.browser_click(ctx, ref=ref_of(snap, "Trattoria Uno")))
    assert "Select a time" in snap["title"] and "Party of 4" in snap["text"]

    slot = ref_of(snap, "7:00 PM")
    assert run(browser._click_gate(ctx, {"ref": slot})) is None  # picking a slot isn't final
    snap = run(browser.browser_click(ctx, ref=slot))
    assert "Complete your reservation" in snap["title"]
    assert "7:00 PM" in snap["text"] and "Party of 4" in snap["text"]

    pw_ref = ref_of(snap, "Create a password")
    cc_ref = ref_of(snap, "Card number")
    cvv_ref = ref_of(snap, "CVV")
    assert element(snap, pw_ref)["sensitive"] == "password"
    assert element(snap, cc_ref)["sensitive"] == "payment card"
    assert element(snap, cvv_ref)["sensitive"] == "payment card"
    assert "password_field" in snap and "blocker" not in snap

    # Never type secrets, through any route.
    for ref in (pw_ref, cc_ref, cvv_ref):
        with pytest.raises(ToolError) as err:
            run(browser.browser_type(ctx, ref=ref, text="secret123"))
        assert "ask the user" in str(err.value)
    out = json.loads(run(call_tool("browser_type", {"ref": pw_ref, "text": "hunter2"}, ctx)))
    assert "password" in out["error"]
    with pytest.raises(ToolError):
        run(browser.browser_type(ctx, ref=ref_of(snap, "First name"), text="4111 1111 1111 1111"))
    run(browser.browser_click(ctx, ref=pw_ref))
    with pytest.raises(ToolError):
        run(browser.browser_press(ctx, key="a"))
    with pytest.raises(ToolError):
        run(browser.browser_press(ctx, key="Control+v"))

    # Fill the contact details in one go; the card field in the batch is refused, the rest go in.
    snap = run(browser.browser_fill(ctx, fields=[
        {"ref": ref_of(snap, "First name"), "value": "Alex"},
        {"ref": ref_of(snap, "Last name"), "value": "Rivera"},
        {"ref": ref_of(snap, "Phone number"), "value": "555-0134"},
        {"ref": ref_of(snap, "Email"), "value": "alex@example.com"},
        {"ref": ref_of(snap, "Text me updates"), "value": "true"},
        {"ref": cc_ref, "value": "4242424242424242"},
    ]))
    assert snap["status"] == "Filled 5 of 6 field(s)"
    assert len(snap["errors"]) == 1 and "payment card" in snap["errors"][0]
    assert element(snap, ref_of(snap, "First name"))["value"] == "Alex"
    assert element(snap, ref_of(snap, "Text me updates"))["checked"] is True
    assert "value" not in element(snap, cc_ref)

    # The user types their own password in the window: ATLAS never reads it back.
    run(browser._call(lambda: browser._S.page.fill("#pw", "hunter2")))
    snap = run(browser.browser_snapshot(ctx))
    assert "hunter2" not in json.dumps(snap)

    # Enter in the email field would submit the reservation: gated, like the button itself.
    email = ref_of(snap, "Email")
    card = run(browser._type_gate(ctx, {"ref": email, "text": "alex@example.com", "submit": True}))
    assert card and "Complete reservation" in json.dumps(card)
    run(browser.browser_click(ctx, ref=email))
    assert run(browser._press_gate(ctx, {"key": "Enter"}))
    assert run(browser._press_gate(ctx, {"key": "Tab"})) is None

    done = ref_of(snap, "Complete reservation")
    card = run(browser._click_gate(ctx, {"ref": done}))
    assert card is not None
    fields = dict(card["fields"])
    assert fields["Site"] == "127.0.0.1"
    assert "Complete your reservation" in fields["Page"]
    assert "Complete reservation" in card["title"]
    assert "Party of 4" in card["body"] and "7:00 PM" in card["body"]
    assert "Alex" in card["body"] and "hunter2" not in card["body"]

    # The user approved: the click goes through.
    snap = run(browser.browser_click(ctx, ref=done))
    assert snap["title"] == "Confirmed"
    assert "Confirmation #TU-4821 for Alex, party of 4 at 7:00 PM" in snap["text"]
    assert "Card on file: no" in snap["text"]

    snap = run(browser.browser_back(ctx))
    assert "Complete your reservation" in snap["title"]


def test_refs_are_stable_and_stale_refs_fail(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/"))
    again = run(browser.browser_snapshot(ctx))
    assert [e["ref"] for e in snap["elements"]] == [e["ref"] for e in again["elements"]]
    with pytest.raises(ToolError) as err:
        run(browser.browser_click(ctx, ref="e999"))
    assert "browser_snapshot" in str(err.value)
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref="not a ref"))
    assert run(browser.browser_click(ctx, ref=ref_of(snap, "Tall page")))["title"] == "Tall"


def test_scroll_and_in_view(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/tall"))
    deep = ref_of(snap, "Deep button")
    assert element(snap, deep)["in_view"] is False
    assert snap["scroll"] == "at top (more below)"
    snap = run(browser.browser_scroll(ctx, direction="bottom"))
    assert "in_view" not in element(snap, deep)
    assert snap["scroll"] == "at bottom"
    snap = run(browser.browser_scroll(ctx, direction="up", amount=10))
    assert snap["scroll"] == "at top (more below)"
    snap = run(browser.browser_scroll(ctx, direction="down", amount=1))
    assert snap["scroll"].endswith("% down") or snap["scroll"] == "at bottom"


def test_snapshot_fits_tool_result_limit(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/many"))
    size = len(json.dumps(snap, ensure_ascii=False))
    assert size <= browser.SNAPSHOT_BUDGET < 14000
    assert "not shown" in str(snap["elements_total"])
    assert len(snap["elements"]) > 40
    full = run(browser.browser_snapshot(ctx, full_text=True))
    assert len(full["text"]) > 5000 and len(json.dumps(full, ensure_ascii=False)) <= browser.SNAPSHOT_BUDGET


def test_cross_origin_iframe_widget(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/frames"))
    reserve = ref_of(snap, "Reserve now")
    assert reserve.startswith("f")
    assert any("widget" in f["text"] for f in snap.get("frames", []))
    card = run(browser._click_gate(ctx, {"ref": reserve}))
    assert card is not None and "Party of 2" in card["body"]
    assert dict(card["fields"])["Inside"] == "embedded frame from localhost"
    snap = run(browser.browser_click(ctx, ref=reserve))
    assert any("Booked via widget" in f["text"] for f in snap["frames"])


def test_popup_tab_is_followed_and_left(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/popup"))
    snap = run(browser.browser_click(ctx, ref=ref_of(snap, "Open results in new tab")))
    assert snap["title"] == "Results for popup"
    assert any("new tab" in e for e in snap["events"])
    run(browser._call(lambda: browser._S.page.close()))
    snap = run(browser.browser_snapshot(ctx))
    assert snap["title"] == "Popup"  # back on the tab that opened it


def test_confirm_dialogs_are_dismissed_and_reported(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/dialog"))
    snap = run(browser.browser_click(ctx, ref=ref_of(snap, "Ask me")))
    assert snap["title"] == "no"
    assert any("Really start over?" in e for e in snap["events"])


def test_chat_composer_enter_is_gated(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/chat"))
    msg = ref_of(snap, "Message")
    assert run(browser._type_gate(ctx, {"ref": msg, "text": "hi", "submit": True}))
    assert run(browser._type_gate(ctx, {"ref": msg, "text": "hi", "submit": False})) is None


def test_browser_look_uses_vision_with_ref_tags(live, ctx):
    run(browser.browser_open(ctx, url=live + "/details?time=7:00 PM&party=2"))
    out = run(browser.browser_look(ctx, question="Where is the final button?"))
    assert out["observation"] == "The Complete reservation button is tagged e9."
    call = ctx.llm.calls[-1]
    assert call["model"] == "test/vision-model"
    parts = call["messages"][0]["content"]
    assert "red tags" in parts[0]["text"] and "untrusted" in parts[0]["text"]
    data_url = parts[1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1])[:2] == b"\xff\xd8"
    assert run(browser._call(lambda: browser._S.page.evaluate("!!document.getElementById('__atlas_overlay__')"))) is False


def test_reservation_search_opens_primary_link(live, ctx, monkeypatch):
    real = browser.reservation_links

    def local_links(*args):
        links = real(*args)
        links["opentable"] = live + "/search?q=italian&party=2"
        return links

    monkeypatch.setattr(browser, "reservation_links", local_links)
    out = run(browser.reservation_search(ctx, query="italian", city="Boston", party_size=2))
    assert out["status"].startswith("Opened OpenTable results")
    assert out["snapshot"]["title"] == "Results for italian"
    assert ref_of(out["snapshot"], "Trattoria Uno")
    assert set(out["links"]) == {"opentable", "resy", "google_maps", "yelp", "google"}
    assert len(json.dumps(out, ensure_ascii=False)) < 14000


def test_recovers_when_user_closes_window(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/"))
    old_ref = ref_of(snap, "Tall page")

    # The user closes the window (the browser process goes away behind ATLAS's back).
    def kill():
        cdp = browser._S.context.new_cdp_session(browser._S.page)
        try:
            cdp.send("Browser.close")
        except Exception:
            pass

    run(browser._call(kill))
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=old_ref))
    snap = run(browser.browser_open(ctx, url=live + "/tall"))  # relaunches
    assert snap["title"] == "Tall"

    # Closing only ATLAS's tab: it opens a fresh one.
    run(browser._call(lambda: browser._S.page.close()))
    assert run(browser.browser_open(ctx, url=live + "/"))["title"] == "TableFinder"

    # browser_close, then reopen.
    assert "Closed" in run(browser.browser_close(ctx))["status"]
    assert "wasn't open" in run(browser.browser_close(ctx))["status"]
    with pytest.raises(ToolError):
        run(browser.browser_snapshot(ctx))
    assert run(browser.browser_open(ctx, url=live + "/"))["title"] == "TableFinder"


def test_shadow_dom_custom_widgets_and_hidden_elements(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/widgets"))
    labels = [e.get("label") for e in snap["elements"]]
    assert "Hidden thing" not in labels
    assert "8:15 PM" in labels  # a cursor:pointer div with no role still gets a ref
    day = element(snap, ref_of(snap, "14"))
    assert day["role"] == "gridcell" and day["selected"] is True
    editor = element(snap, ref_of(snap, "Special requests"))
    snap = run(browser.browser_type(ctx, ref=editor["ref"], text="Window seat, please"))
    assert element(snap, editor["ref"])["value"] == "Window seat, please"
    # Elements inside open shadow roots get refs, are gated like any other, and can be clicked.
    shadow = ref_of(snap, "Book this table")
    assert run(browser._click_gate(ctx, {"ref": shadow}))
    assert run(browser.browser_click(ctx, ref=shadow))["title"] == "shadow clicked"
    assert run(browser._click_gate(ctx, {"ref": ref_of(snap, "8:15 PM")})) is None


# ----------------------------------------------------------------------------- adversarial regressions
def _js(expr):
    return run(browser._call(lambda: browser._S.page.evaluate(expr)))


def test_typing_never_clicks_buttons_or_reaches_secret_fields(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/attack"))
    posts = len(FakeSite.posts)
    submit = ref_of(snap, "Complete reservation", tag="input")
    # browser_type / browser_fill on a submit button used to fall back to clicking it: the reservation was made
    # without any approval card.
    with pytest.raises(ToolError) as err:
        run(browser.browser_type(ctx, ref=submit, text="x"))
    assert "browser_click" in str(err.value)
    snap = run(browser.browser_fill(ctx, fields=[{"ref": submit, "value": "x"}]))
    assert snap["status"] == "Filled 0 of 1 field(s)" and "button" in snap["errors"][0]
    with pytest.raises(ToolError):
        run(browser.browser_type(ctx, ref=ref_of(snap, "Home page"), text="x"))
    assert len(FakeSite.posts) == posts and snap["title"] == "Attack"

    # A label (or a widget that hands focus over) must not become a way into the password box.
    with pytest.raises(ToolError) as err:
        run(browser.browser_type(ctx, ref=ref_of(snap, "Your password", tag="label"), text="hunter2"))
    assert "never enters password" in str(err.value)
    with pytest.raises(ToolError) as err:
        run(browser.browser_type(ctx, ref=ref_of(snap, "Sneaky"), text="hunter2"))
    assert "never enters password" in str(err.value)
    assert _js("document.getElementById('pw2').value") == ""

    # A password field switched to plain text is still a secret: never shown, never typed into.
    shown = element(snap, ref_of(snap, "Password (shown)"))
    assert shown["sensitive"] == "password" and "value" not in shown
    assert element(snap, ref_of(snap, "Choose one"))["sensitive"] == "password"  # judged by its name alone
    assert "hunter2" not in json.dumps(snap) and "s3cret" not in json.dumps(snap)
    with pytest.raises(ToolError):
        run(browser.browser_type(ctx, ref=shown["ref"], text="hunter3"))

    # Keyboard typing into a custom widget must not turn a newline into an Enter press.
    notes = ref_of(snap, "Notes")
    snap = run(browser.browser_type(ctx, ref=notes, text="hello\nworld"))
    assert snap["title"] == "Attack"
    assert element(snap, notes).get("label") == "Notes"
    assert _js("window.__keys") == "hello world"
    # submit="false" (a string) means false.
    email = ref_of(snap, "Email")
    assert run(browser._type_gate(ctx, {"ref": email, "text": "a@b.c", "submit": "false"})) is None
    run(browser.browser_type(ctx, ref=email, text="a@b.c", submit="false"))
    assert len(FakeSite.posts) == posts


def test_final_actions_are_rechecked_when_they_run(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/details?time=7:00 PM&party=2"))
    posts = len(FakeSite.posts)
    done = ref_of(snap, "Complete reservation")
    email = ref_of(snap, "Email")
    # No card was shown for this click (e.g. its gate ran before an earlier action changed the page): refused.
    with pytest.raises(ToolError) as err:
        run(browser.browser_click(ctx, ref=done))
    assert "approval" in str(err.value) and f"ref='{done}'" in str(err.value)
    # Parallel calls [click the email field, press Enter]: the press gate saw nothing focused, so it said None.
    assert run(browser._press_gate(ctx, {"key": "Enter"})) is None
    run(browser.browser_click(ctx, ref=email))
    with pytest.raises(ToolError):
        run(browser.browser_press(ctx, key="Enter"))
    with pytest.raises(ToolError):
        run(browser.browser_type(ctx, ref=email, text="alex@example.com", submit=True))
    assert len(FakeSite.posts) == posts
    # With the card shown (and approved), the same press goes through.
    run(browser.browser_fill(ctx, fields=[{"ref": ref_of(snap, "First name"), "value": "Alex"}]))
    run(browser.browser_click(ctx, ref=email))
    assert run(browser._press_gate(ctx, {"key": "Enter"}))
    snap = run(browser.browser_press(ctx, key="Enter"))
    assert snap["title"] == "Confirmed" and FakeSite.posts[-1] == "/complete"


def test_approval_covers_only_the_element_that_was_shown(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/formless"))
    later = ref_of(snap, "View menu")
    order = ref_of(snap, "Place order")
    # Harmless when checked, made final afterwards: the click is refused.
    assert run(browser._click_gate(ctx, {"ref": later})) is None
    _js("document.getElementById('later').textContent = 'Place order'")
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=later))
    assert run(browser._click_gate(ctx, {"ref": later}))
    assert run(browser.browser_click(ctx, ref=later))["title"] == "clicked later"
    _js("document.title = 'Formless'")
    # Approved as "Place order", relabelled before it ran: refused.
    assert run(browser._click_gate(ctx, {"ref": order}))
    _js("document.querySelector('.promo button').textContent = 'Delete account'")
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=order))
    _js("document.querySelector('.promo button').textContent = 'Place order'")
    # A card the user denied leaves nothing usable once the next check finds the element gone.
    assert run(browser._click_gate(ctx, {"ref": order}))
    _js(f"document.querySelector('[data-atlas-ref={order}]').removeAttribute('data-atlas-ref')")
    assert run(browser._click_gate(ctx, {"ref": order})) is None
    _js(f"document.querySelector('.promo button').setAttribute('data-atlas-ref', '{order}')")
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=order))
    assert _js("document.title") == "Formless"
    # Approvals are one-shot.
    assert run(browser._click_gate(ctx, {"ref": order}))
    assert run(browser.browser_click(ctx, ref=order))["title"] == "ordered"
    _js("document.title = 'Formless'")
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=order))
    # The user's AUTO_APPROVE setting is the one way past the card.
    auto = ToolContext(settings=Settings(auto_approve={"browser_click"}), http=ctx.http, llm=ctx.llm)
    assert run(browser.browser_click(auto, ref=order))["title"] == "ordered"


def test_formless_fields_widgets_and_card_numbers(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/formless"))
    promo = ref_of(snap, "Promo code")
    # Enter in a field that isn't in a <form> but sits next to "Place order" is gated; next to "Search" it isn't.
    assert run(browser._type_gate(ctx, {"ref": promo, "text": "SAVE10", "submit": True}))
    assert run(browser._type_gate(ctx, {"ref": ref_of(snap, "Search dishes"), "text": "pasta", "submit": True})) is None
    # Ctrl/Cmd+Enter in a message box usually sends it.
    run(browser.browser_click(ctx, ref=ref_of(snap, "Message body")))
    assert run(browser._press_gate(ctx, {"key": "Control+Enter"}))
    assert run(browser._press_gate(ctx, {"key": "Tab"})) is None
    # A custom checkbox whose label is a final action isn't ticked through browser_fill.
    snap = run(browser.browser_fill(ctx, fields=[{"ref": ref_of(snap, "Subscribe to offers"), "value": "true"}]))
    assert "browser_click" in snap["errors"][0]
    # Clicking a label activates its control: a label in front of a hidden "Buy now" button is gated too.
    cont = ref_of(snap, "Continue", tag="label")
    with pytest.raises(ToolError):
        run(browser.browser_click(ctx, ref=cont))  # no card was shown
    assert _js("document.title") == "Formless"
    card = run(browser._click_gate(ctx, {"ref": cont}))
    assert card and "buy" in dict(card["fields"])["Why asking"]
    assert run(browser.browser_click(ctx, ref=cont))["title"] == "bought"  # approved
    # A card number the user typed into an unlabelled field is never shown to the model.
    run(browser._call(lambda: browser._S.page.fill("#refno", "4111 1111 1111 1111")))
    snap = run(browser.browser_snapshot(ctx))
    assert "4111" not in json.dumps(snap)
    assert "card number" in element(snap, ref_of(snap, "Reference"))["value"]


def test_redirect_into_private_network_is_blocked(live, ctx, monkeypatch):
    monkeypatch.setattr(browser, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(browser, "_check_url", lambda url: url)  # pretend the start address is public
    with pytest.raises(ToolError) as err:
        run(browser.browser_open(ctx, url=live + "/go-home"))
    assert "private" in str(err.value)
    assert run(browser._call(lambda: browser._S.page.url)) == "about:blank"


def test_gate_fails_closed_when_inspection_breaks_or_worker_is_busy(live, ctx, monkeypatch):
    snap = run(browser.browser_open(ctx, url=live + "/formless"))
    order = ref_of(snap, "Place order")
    real_inspect = browser._inspect

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(browser, "_inspect", broken)
    card = run(browser._click_gate(ctx, {"ref": order}))
    assert card["title"] == "Approve browser action" and "couldn't check" in card["body"]
    monkeypatch.setattr(browser, "_inspect", real_inspect)
    assert run(browser.browser_click(ctx, ref=order))["title"] == "ordered"  # the user approved the generic card

    # A busy (or frozen) worker must not stall the agent's approval step, nor wave the action through.
    _js("document.title = 'Formless'")
    monkeypatch.setattr(browser, "GATE_TIMEOUT", 0.4)

    async def scenario():
        slow = asyncio.ensure_future(browser._call(time.sleep, 1.5, timeout=10))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        card = await browser._click_gate(ctx, {"ref": order})
        elapsed = time.monotonic() - t0
        await slow
        return card, elapsed

    card, elapsed = run(scenario())
    assert elapsed < 1.2 and card["title"] == "Approve browser action"
    assert run(browser.browser_click(ctx, ref=order))["title"] == "ordered"


def test_queued_job_is_skipped_after_its_caller_gave_up():
    ran = []

    async def scenario():
        slow = asyncio.ensure_future(browser._call(time.sleep, 1.0, timeout=10))
        await asyncio.sleep(0.05)
        with pytest.raises(ToolError) as err:
            await browser._call(lambda: ran.append(1), timeout=0.3)
        await slow
        await browser._call(lambda: None)  # let the queue drain
        return str(err.value)

    msg = run(scenario())
    assert "didn't respond in time" in msg
    assert ran == []  # it never ran late
    # A job that only reaches the worker after its deadline refuses to act.
    with pytest.raises(ToolError) as err:
        browser._job(time.monotonic() - 1, ran.append, 1)
    assert "Skipped" in str(err.value) and ran == []


def test_browser_look_reports_vision_model_errors(live, ctx):
    class NoVision(FakeLLM):
        async def complete(self, messages, model, **extra):
            raise LLMError("OpenRouter error 404: No endpoints found that support image input")

    run(browser.browser_open(ctx, url=live + "/"))
    bad = ToolContext(settings=ctx.settings, http=ctx.http, llm=NoVision())
    out = json.loads(run(call_tool("browser_look", {"question": "What is here?"}, bad)))
    assert "BROWSER_VISION_MODEL" in out["error"] and "image input" in out["error"]
    # The overlay is removed even though the vision call failed.
    assert _js("!!document.getElementById('__atlas_overlay__')") is False


def test_approval_card_shows_the_booking_dialog_not_the_header(live, ctx):
    snap = run(browser.browser_open(ctx, url=live + "/modal"))
    card = run(browser._click_gate(ctx, {"ref": ref_of(snap, "Reserve Now")}))
    assert card and "8:30 PM · 4 guests" in card["body"]
    assert "Neighbourhood guide" not in card["body"]
