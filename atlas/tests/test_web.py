import json

import httpx
import pytest

from app.tools import call_tool
from app.tools.web import _ddg_link, parse_ddg
from conftest import Router, json_response, make_ctx, mock_http, run

DDG_HTML = """
<!DOCTYPE html><html><body><div id="links" class="results">
<div class="result results_links results_links_deep result--ad ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/y.js?ad_domain=shop.example&amp;ad_provider=bing&amp;u3=x">Buy Rates Now</a></h2>
    <a class="result__snippet" href="//duckduckgo.com/y.js?ad=1">Sponsored &amp; promoted</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.federalreserve.gov%2Fnewsevents%2Fpressreleases%2Fmonetary20261104a.htm&amp;rut=abc123">Federal Reserve issues <b>FOMC</b> statement</a>
    </h2>
    <a class="result__url" href="//duckduckgo.com/l/?uddg=x">www.federalreserve.gov</a>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">The Committee decided to maintain the target range &amp; <b>noted</b> that inflation has eased.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fsearch%3Fq%3Da%2526b%26page%3D2%2520x&amp;rut=def">Encoded &quot;target&quot;</a></h2>
    <div class="result__snippet">Snippet in a div with <b>bold</b> text.</div>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://direct.example.org/page">Direct link</a></h2>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="/l/?uddg=https%3A%2F%2Frelative.example%2F&amp;rut=1">Relative redirect</a></h2>
</div>
</div></body></html>
"""


def test_parse_ddg_realistic():
    results = parse_ddg(DDG_HTML, 10)
    assert [r["title"] for r in results] == [
        "Federal Reserve issues FOMC statement",
        'Encoded "target"',
        "Direct link",
        "Relative redirect",
    ]
    fed, encoded, direct, relative = results
    assert fed["url"] == "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261104a.htm"
    assert fed["snippet"] == "The Committee decided to maintain the target range & noted that inflation has eased."
    # uddg is decoded exactly once: %26 inside the target's query must survive
    assert encoded["url"] == "https://example.com/search?q=a%26b&page=2%20x"
    assert encoded["snippet"] == "Snippet in a div with bold text."
    assert direct["url"] == "https://direct.example.org/page" and direct["snippet"] == ""
    assert relative["url"] == "https://relative.example/"


def test_parse_ddg_limit_and_empty():
    assert len(parse_ddg(DDG_HTML, 2)) == 2
    assert parse_ddg("<html>No results.</html>", 5) == []


def test_ddg_link_passthrough():
    assert _ddg_link("https://example.com/a") == "https://example.com/a"
    assert _ddg_link("//duckduckgo.com/l/?rut=x") == "https://duckduckgo.com/l/?rut=x"


class SearchLLM:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    async def resolve_search_model(self):
        return "google/gemini-2.5-flash"

    async def complete(self, messages, model, **extra):
        self.calls.append((messages, model, extra))
        if self.exc:
            raise self.exc
        return self.data


def test_web_search_openrouter_answer_with_sources():
    llm = SearchLLM(
        {
            "choices": [
                {
                    "message": {
                        "content": "The Fed held rates at 4.25-4.5%.",
                        "annotations": [
                            {"type": "url_citation", "url_citation": {"url": "https://reuters.com/a", "title": "Reuters"}},
                            {"type": "url_citation", "url_citation": {"url": "https://reuters.com/a", "title": "dup"}},
                            {"type": "url_citation", "url_citation": {"url": "https://apnews.com/b"}},
                        ],
                    }
                }
            ]
        }
    )

    async def go():
        async with mock_http(Router()) as http:
            return json.loads(await call_tool("web_search", {"query": "fed decision", "max_results": 50}, make_ctx(http=http, llm=llm)))

    out = run(go())
    assert out["answer"].startswith("The Fed held")
    assert out["sources"] == [{"title": "Reuters", "url": "https://reuters.com/a"}, {"title": "apnews.com", "url": "https://apnews.com/b"}]
    assert out["engine"] == "openrouter-web (google/gemini-2.5-flash)"
    _, model, extra = llm.calls[0]
    assert extra["plugins"] == [{"id": "web", "max_results": 10}]  # clamped


def test_web_search_falls_back_to_duckduckgo():
    router = Router({"html.duckduckgo.com/html/": httpx.Response(200, text=DDG_HTML)})

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("web_search", {"query": "fomc"}, make_ctx(http=http, llm=SearchLLM(exc=RuntimeError("no key")))))

    out = run(go())
    assert out["engine"] == "duckduckgo" and len(out["results"]) == 4
    assert router.requests[0].method == "POST"


def test_web_search_both_fail():
    router = Router({"html.duckduckgo.com/html/": httpx.Response(200, text="<html></html>")})

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("web_search", {"query": "x"}, make_ctx(http=http, llm=SearchLLM(data={"choices": [{"message": {"content": ""}}]}))))

    out = run(go())
    assert out["error"].startswith("Web search failed")
    assert "no results" in out["error"]


ARTICLE = "<html><head><title>Big &amp; News</title></head><body><nav>menu</nav><article>" + ("<p>Paragraph of real content.</p>" * 80) + "</article></body></html>"


def fetch(url, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
            return json.loads(await call_tool("fetch_url", {"url": url}, make_ctx(http=http)))

    return run(go())


def test_fetch_url_reads_article():
    out = fetch("https://example.com/story", lambda r: httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"}))
    assert out["title"] == "Big & News"
    assert out["content"].startswith("Paragraph of real content.")
    assert "menu" not in out["content"]


def test_fetch_url_plain_text():
    out = fetch("https://example.com/file.txt", lambda r: httpx.Response(200, text="hello world", headers={"content-type": "text/plain"}))
    assert out == {"url": "https://example.com/file.txt", "title": "", "content": "hello world"}


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/api/state", "file:///etc/passwd", "http://192.168.1.1/", "http://localhost/"])
def test_fetch_url_rejects_private_targets(url):
    out = fetch(url, lambda r: pytest.fail("must not send a request"))
    assert out["error"] == "Only public http(s) URLs can be fetched."


def test_fetch_url_follows_public_redirects():
    def handler(r):
        if r.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="moved here", headers={"content-type": "text/plain"})

    out = fetch("https://example.com/old", handler)
    assert out["url"] == "https://example.com/new" and out["content"] == "moved here"


@pytest.mark.parametrize("target", ["http://127.0.0.1:8765/api/state", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"])
def test_fetch_url_blocks_redirect_to_private(target):
    hits = []

    def handler(r):
        hits.append(str(r.url))
        if r.url.host == "evil.example":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text="SECRET")

    out = fetch("https://evil.example/x", handler)
    assert "redirects to a private" in out["error"]
    assert hits == ["https://evil.example/x"]


def test_fetch_url_too_many_redirects():
    out = fetch("https://loop.example/a", lambda r: httpx.Response(302, headers={"location": "https://loop.example/a"}))
    assert out["error"] == "Too many redirects."


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["93.184.215.14", "192.168.1.20"],  # any private address in the answer is enough
        ["::1"],
        ["fe80::1%eth0"],
        ["::ffff:127.0.0.1"],
        ["100.64.0.7"],
    ],
)
def test_fetch_url_rejects_names_that_resolve_privately(fake_dns, answer):
    fake_dns["127.0.0.1.nip.io"] = answer
    out = fetch("http://127.0.0.1.nip.io:8765/api/state", lambda r: pytest.fail("must not send a request"))
    assert out["error"] == "That address points to this computer or a private network, so it can't be fetched."


def test_fetch_url_resolves_the_host_the_client_will_connect_to(monkeypatch):
    import app.util as util

    asked, seen = [], []
    monkeypatch.setattr(util, "_getaddrinfo", lambda host, port: asked.append((host, port)) or ["93.184.215.14"])

    def handler(r):
        seen.append(r.url.raw_host)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    out = fetch("https://bücher.de/x", handler)
    assert out["content"] == "ok"
    assert asked == [("xn--bcher-kva.de", 443)] and seen == [b"xn--bcher-kva.de"]


@pytest.mark.parametrize("url", ["http://127。0。0。1:8765/api/state", "http://①②⑦.0.0.1/", "http://ｌｏｃａｌｈｏｓｔ/"])
def test_fetch_url_rejects_unicode_spellings_of_local_hosts(url):
    out = fetch(url, lambda r: pytest.fail("must not send a request"))
    assert out["error"] == "Only public http(s) URLs can be fetched."


def test_fetch_url_unknown_host(fake_dns):
    import socket

    fake_dns["no-such-site.example"] = socket.gaierror(-2, "Name or service not known")
    out = fetch("https://no-such-site.example/", lambda r: pytest.fail("must not send a request"))
    assert out["error"].startswith("Couldn't find the site no-such-site.example")


def test_fetch_url_blocks_redirect_to_name_resolving_privately(fake_dns):
    fake_dns["internal.attacker.example"] = ["10.0.0.8"]
    hits = []

    def handler(r):
        hits.append(str(r.url))
        if r.url.host == "evil.example":
            return httpx.Response(302, headers={"location": "http://internal.attacker.example/admin"})
        return httpx.Response(200, text="SECRET")

    out = fetch("https://evil.example/x", handler)
    assert out["error"] == "That page redirects to a private or non-web address, so it can't be fetched."
    assert hits == ["https://evil.example/x"]


def test_fetch_url_revalidates_every_hop(fake_dns):
    fake_dns["c.example"] = ["127.0.0.1"]
    hits = []

    def handler(r):
        hits.append(r.url.host)
        nxt = {"a.example": "https://b.example/2", "b.example": "//c.example/3"}.get(r.url.host)
        return httpx.Response(307, headers={"location": nxt}) if nxt else httpx.Response(200, text="SECRET")

    out = fetch("https://a.example/1", handler)
    assert "redirects to a private" in out["error"]
    assert hits == ["a.example", "b.example"]


def test_fetch_url_never_auto_follows_on_a_redirect_following_client():
    hits = []

    def handler(r):
        hits.append(str(r.url))
        if r.url.path == "/old":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/state"})
        return httpx.Response(200, text="SECRET")

    out = fetch("https://example.com/old", handler)  # fetch() builds the client with follow_redirects=True
    assert "redirects to a private" in out["error"] and hits == ["https://example.com/old"]


def test_fetch_url_follows_five_redirects_but_not_six():
    def handler(limit):
        def h(r):
            n = int(r.url.path.strip("/") or 0)
            if n < limit:
                return httpx.Response(302, headers={"location": f"/{n + 1}"})
            return httpx.Response(200, text=f"arrived {n}", headers={"content-type": "text/plain"})

        return h

    assert fetch("https://hops.example/0", handler(5))["content"] == "arrived 5"
    assert fetch("https://hops.example/0", handler(6))["error"] == "Too many redirects."


def test_fetch_url_stops_downloading_after_the_size_cap():
    sent = []

    async def body():
        yield b"<html><body><p>Short visible text.</p>"
        for _ in range(200):  # 200 x 64 KB = 12.8 MB of markup if it were read to the end
            sent.append(1)
            yield b"<!--" + b"x" * (64 * 1024 - 7) + b"-->"

    out = fetch("https://huge.example/", lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=body()))
    assert out["content"].startswith("Short visible text.")
    assert out["note"] == "Only the first 2 MB of this page was read."
    assert len(sent) <= 32  # stopped at ~2 MB instead of downloading 12.8 MB


def test_fetch_url_small_page_has_no_truncation_note():
    out = fetch("https://example.com/", lambda r: httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"}))
    assert "note" not in out


def test_fetch_public_caps_bytes_exactly():
    from app.tools.web import fetch_public

    async def go():
        async def body():
            for _ in range(10):
                yield b"x" * 1000

        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body()))
        async with httpx.AsyncClient(transport=transport) as http:
            r, raw, truncated = await fetch_public(http, "https://example.com/", max_bytes=2500)
            return raw, truncated

    raw, truncated = run(go())
    assert raw == b"x" * 2500 and truncated is True


def test_fetch_url_decodes_declared_charset():
    page = "<html><head><title>Café</title></head><body><p>Crème brûlée</p></body></html>".encode("latin-1")
    out = fetch("https://example.fr/", lambda r: httpx.Response(200, content=page, headers={"content-type": "text/html; charset=ISO-8859-1"}))
    assert out["title"] == "Café" and "Crème brûlée" in out["content"]


def test_fetch_url_unknown_charset_falls_back_to_utf8():
    out = fetch("https://example.com/", lambda r: httpx.Response(200, content="héllo".encode(), headers={"content-type": "text/plain; charset=x-bogus"}))
    assert out["content"] == "héllo"


def test_fetch_url_google_news_redirect():
    out = fetch("https://news.google.com/rss/articles/abc", lambda r: httpx.Response(200, text="<html>js redirect</html>"))
    assert "Google News redirect links" in out["error"]


def test_wikipedia():
    router = Router(
        {
            "en.wikipedia.org/w/api.php": json_response(["Ada", ["Ada Lovelace", "Ada (language)"], [], []]),
            "en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace": json_response(
                {"title": "Ada Lovelace", "description": "English mathematician", "extract": "Augusta Ada King...", "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}}}
            ),
        }
    )

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("wikipedia", {"query": "Ada"}, make_ctx(http=http)))

    out = run(go())
    assert out["title"] == "Ada Lovelace" and out["other_matches"] == ["Ada (language)"]
    assert out["url"].endswith("/Ada_Lovelace")


def test_wikipedia_no_match():
    router = Router({"en.wikipedia.org/w/api.php": json_response(["zzz", [], [], []])})

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("wikipedia", {"query": "zzz"}, make_ctx(http=http)))

    assert "No Wikipedia article" in run(go())["error"]
