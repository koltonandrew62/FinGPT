from datetime import datetime, timedelta, timezone

import pytest

from app.util import clip, html_to_text, is_public_http_url, relative_time


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
