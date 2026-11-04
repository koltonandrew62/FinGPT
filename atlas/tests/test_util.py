from datetime import datetime, timedelta, timezone

import pytest

import socket

from app import util
from app.util import NonPublicAddress, clip, html_to_text, is_public_http_url, is_public_ip, relative_time, resolves_public
from conftest import run

REAL_GETADDRINFO = util._getaddrinfo


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://LOCALHOST:8765/api/state",
        "http://localhost./",
        "http://printer.local/",
        "http://app.localhost/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8765/api/state",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://0.0.0.0/",
        "http://10.0.0.5/",
        "http://172.16.3.4/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[::1]:8765/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://224.0.0.1/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://example.com/file",
        "http://",
        "not a url",
        "",
        "http://[::1/",
        "http://127。0。0。1/",  # ideographic full stops: IDNA/browsers turn this into 127.0.0.1
        "http://127．0．0．1/",
        "http://１２７.0.0.1/",
        "http://ｌｏｃａｌｈｏｓｔ/",
        "http://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
        "http://[2002:c0a8:101::]/",  # 6to4 of 192.168.1.1
    ],
)
def test_blocks_local_private_and_non_http(url):
    assert is_public_http_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://news.bbc.co.uk/path?q=1",
        "https://8.8.8.8/",
        "https://[2606:4700:4700::1111]/",
        "HTTPS://Example.COM/Upper",
        "https://cafe.be/",
    ],
)
def test_allows_public_http(url):
    assert is_public_http_url(url) is True


@pytest.mark.parametrize(
    "addr, public",
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:8.8.8.8", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("::", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd12::1", False),
        ("::ffff:10.0.0.1", False),
        ("2001:0:4136:e378:8000:63bf:80ff:fffe", False),  # Teredo with a 127.0.0.1 client
        ("not an ip", False),
    ],
)
def test_is_public_ip(addr, public):
    assert is_public_ip(addr) is public


def test_resolves_public_checks_every_address(fake_dns):
    fake_dns["ok.example"] = ["93.184.215.14", "2606:2800:220:1::"]
    fake_dns["mixed.example"] = ["93.184.215.14", "127.0.0.1"]
    fake_dns["empty.example"] = []
    assert run(resolves_public("https://ok.example/a")) == "ok.example"
    for host in ("mixed.example", "empty.example"):
        with pytest.raises(NonPublicAddress):
            run(resolves_public(f"https://{host}/"))


def test_resolves_public_rejects_literals_and_non_http_without_lookup(monkeypatch):
    monkeypatch.setattr(util, "_getaddrinfo", lambda host, port: pytest.fail(f"looked up {host}"))
    for url in ("http://127.0.0.1/", "http://[::1]:80/", "file:///etc/passwd", "http://localhost/", "http://127。0。0。1/"):
        with pytest.raises(NonPublicAddress):
            run(resolves_public(url))
    assert run(resolves_public("https://8.8.8.8/")) == "8.8.8.8"


def test_resolves_public_lookup_failure_propagates(fake_dns):
    fake_dns["nx.example"] = socket.gaierror(-2, "Name or service not known")
    with pytest.raises(OSError):
        run(resolves_public("https://nx.example/"))


def test_resolves_public_passes_the_default_port(monkeypatch):
    asked = []
    monkeypatch.setattr(util, "_getaddrinfo", lambda host, port: asked.append((host, port)) or ["93.184.215.14"])
    run(resolves_public("http://a.example/"))
    run(resolves_public("https://a.example/"))
    run(resolves_public("https://a.example:8443/"))
    assert asked == [("a.example", 80), ("a.example", 443), ("a.example", 8443)]


def test_real_getaddrinfo_helper_on_a_numeric_host():
    # Captured at import, before the autouse fake_dns fixture patches it; a numeric host needs no network.
    assert REAL_GETADDRINFO("127.0.0.1", 80) == ["127.0.0.1"]


def test_html_to_text_strips_noise_and_decodes():
    raw = """
    <html><head><title>T</title><style>body{color:red}</style><script>alert('x')</script></head>
    <body><nav>Menu Home About</nav><header>Site header</header>
    <h1>Big &amp; Bold</h1><!-- hidden comment -->
    <p>First&nbsp;paragraph with <b>bold</b> and <a href="#">a link</a>.</p>
    <ul><li>one</li><li>two</li></ul>
    <p>Caf&eacute; &#8212; &lt;tag&gt;</p>
    <footer>Copyright</footer></body></html>
    """
    text = html_to_text(raw)
    assert "alert" not in text and "color:red" not in text
    assert "Menu" not in text and "Copyright" not in text and "Site header" not in text
    assert "hidden comment" not in text
    assert "Big & Bold" in text
    assert "bold and a link" in text
    assert "Café — <tag>" in text
    lines = text.splitlines()
    assert "one" in lines and "two" in lines
    assert "\n\n\n" not in text


def test_html_to_text_plain_passthrough():
    assert html_to_text("just text") == "just text"
    assert html_to_text("") == ""


def test_clip():
    assert clip("short", 10) == "short"
    assert clip("", 5) == ""
    assert clip(None, 5) == ""
    out = clip("abcdefghij", 5)
    assert out == "abcd…" and len(out) == 5


def test_relative_time():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert relative_time(None) == ""
    assert relative_time(now - timedelta(seconds=30), now) == "just now"
    assert relative_time(now + timedelta(minutes=5), now) == "just now"
    assert relative_time(now - timedelta(minutes=5), now) == "5m ago"
    assert relative_time(now - timedelta(hours=3), now) == "3h ago"
    assert relative_time(now - timedelta(days=2), now) == "2d ago"
    naive = datetime(2026, 11, 4, 11, 0)
    assert relative_time(naive, now) == "1h ago"
