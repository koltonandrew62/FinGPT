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
