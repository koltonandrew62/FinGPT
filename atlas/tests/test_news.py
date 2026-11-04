import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.tools import ToolError, call_tool, news
from app.tools.news import _dedupe, _sort, fetch_headlines, parse_feed
from conftest import Router, json_response, make_ctx, mock_http, run

NOW = datetime.now(timezone.utc)


def rfc822(dt):
    return format_datetime(dt)


GOOGLE_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>Top stories - Google News</title>
  <item>
    <title>Fed holds rates steady as inflation cools - Reuters</title>
    <link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
    <pubDate>{rfc822(NOW - timedelta(hours=1))}</pubDate>
    <description>&lt;a href="https://news.google.com/x"&gt;Fed holds rates steady as inflation cools&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Reuters&lt;/font&gt;</description>
    <source url="https://www.reuters.com">Reuters</source>
  </item>
  <item>
    <title>AT&amp;T&#39;s new plan &amp;amp; more - The Verge</title>
    <link>https://news.google.com/rss/articles/CBMiBBB?oc=5</link>
    <pubDate>{rfc822(NOW - timedelta(hours=3))}</pubDate>
    <description><![CDATA[<p>A <b>much longer</b> description about the carrier&rsquo;s pricing overhaul, with details.</p>]]></description>
    <source url="https://www.theverge.com">The Verge</source>
  </item>
  <item>
    <title>   </title>
    <link>https://example.com/empty</link>
  </item>
</channel>
</rss>"""

BBC_RSS = f"""<rss version="2.0"><channel><title>BBC News - Home</title>
<item><title><![CDATA[Storm batters coast]]></title><link>https://www.bbc.co.uk/news/1</link>
<description><![CDATA[Thousands without power after a powerful storm made landfall overnight.]]></description>
<pubDate>{rfc822(NOW - timedelta(minutes=30))}</pubDate></item>
<item><title>Fed holds rates steady as inflation cools</title><link>https://www.bbc.co.uk/news/2</link>
<pubDate>{rfc822(NOW - timedelta(hours=2))}</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Ars Technica - All content</title>
  <entry>
    <title type="html">Chips &amp;amp; Dips: a &lt;i&gt;deep&lt;/i&gt; dive</title>
    <link rel="replies" href="https://arstechnica.com/comments/1"/>
    <link rel="alternate" href="https://arstechnica.com/article/1"/>
    <updated>2026-11-04T10:00:00Z</updated>
    <summary>A review of the latest processors and what they mean for laptops this year.</summary>
  </entry>
  <entry>
    <title>Second entry</title>
    <link href="https://arstechnica.com/article/2"/>
    <published>2026-11-03T08:30:00+02:00</published>
    <content type="html">&lt;p&gt;Body text that is long enough to keep as a summary.&lt;/p&gt;</content>
  </entry>
</feed>"""


def test_parse_google_news_rss():
    items = parse_feed(GOOGLE_RSS)
    assert len(items) == 2  # whitespace-only title dropped
    first, second = items
    assert first["title"] == "Fed holds rates steady as inflation cools"
    assert first["source"] == "Reuters"
    assert first["summary"] == ""  # Google's description just repeats the headline
    assert first["link"].startswith("https://news.google.com/rss/articles/")
    assert first["published"].tzinfo is not None
    assert abs((first["published"] - (NOW - timedelta(hours=1))).total_seconds()) < 2
    assert second["title"] == "AT&T's new plan & more"
    assert second["source"] == "The Verge"
    assert second["summary"].startswith("A much longer description about the carrier’s pricing")


def test_parse_rss_channel_title_fallback_and_default_source():
    items = parse_feed(BBC_RSS)
    assert [i["source"] for i in items] == ["BBC News", "BBC News"]  # " - Home" suffix cleaned
    assert items[0]["title"] == "Storm batters coast"
    assert items[0]["summary"].startswith("Thousands without power")
    assert parse_feed(BBC_RSS, "Custom")[0]["source"] == "Custom"


def test_parse_atom():
    items = parse_feed(ATOM)
    assert len(items) == 2
    a, b = items
    assert a["title"] == "Chips & Dips: a deep dive"
    assert a["link"] == "https://arstechnica.com/article/1"  # rel=alternate, not the comments link
    assert a["published"] == datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    assert a["source"].startswith("Ars Technica")
    assert a["summary"].startswith("A review of the latest processors")
    assert b["link"] == "https://arstechnica.com/article/2"
    assert b["published"] == datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)
    assert b["summary"] == "Body text that is long enough to keep as a summary."


@pytest.mark.parametrize(
    "text",
    ["", "not xml at all", "<rss><channel><item><title>unclosed", "<html><body>consent page</body></html>", "<rss>&nbsp;</rss>"],
)
def test_parse_malformed_or_foreign(text):
    assert parse_feed(text) == []


def test_parse_tolerates_bom_and_leading_whitespace():
    assert len(parse_feed("﻿\n  " + BBC_RSS.replace("<rss", '<?xml version="1.0"?>\n<rss', 1))) == 2


def test_parse_dates():
    assert news._parse_date(None) is None
    assert news._parse_date("garbage") is None
    assert news._parse_date("Tue, 03 Nov 2026 10:00:00 GMT") == datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    assert news._parse_date("2026-11-03T10:00:00") == datetime(2026, 11, 3, 10, tzinfo=timezone.utc)


def test_dedupe_and_sort():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = [
        {"title": "Fed holds rates!", "published": t0},
        {"title": "fed holds  RATES", "published": t0 + timedelta(hours=5)},
        {"title": "Newest", "published": t0 + timedelta(hours=9)},
        {"title": "Undated", "published": None},
    ]
    deduped = _dedupe(items)
    assert [i["title"] for i in deduped] == ["Fed holds rates!", "Newest", "Undated"]
    assert [i["title"] for i in _sort(deduped)] == ["Newest", "Fed holds rates!", "Undated"]


def test_dedupe_keeps_distinct_non_latin_titles():
    items = [{"title": "東京で大雨警報", "published": None}, {"title": "日経平均が最高値", "published": None}, {"title": "Выборы в Москве", "published": None}]
    assert len(_dedupe(items)) == 3
    assert len(_dedupe(items + [{"title": "東京で大雨警報", "published": None}])) == 3


def rss(titles, base=NOW):
    body = "".join(
        f"<item><title>{t}</title><link>https://x/{n}</link><pubDate>{rfc822(base - timedelta(minutes=n))}</pubDate></item>"
        for n, t in enumerate(titles)
    )
    return httpx.Response(200, text=f"<rss><channel><title>Feed</title>{body}</channel></rss>")


def test_fetch_headlines_google_enough_skips_fallback():
    router = Router({"news.google.com/rss": rss([f"Story {i}" for i in range(12)])})

    async def go():
        async with mock_http(router) as http:
            return await fetch_headlines(make_ctx(http=http), "top", limit=10)

    items = run(go())
    assert len(items) == 10
    assert len(router.requests) == 1
    url = str(router.requests[0].url)
    assert "hl=en-US" in url and "gl=US" in url and "ceid=US:en" in url


def test_fetch_headlines_falls_back_and_merges():
    router = Router(
        {
            "news.google.com/rss/headlines/section/topic/TECHNOLOGY": rss(["Only one"]),
            "feeds.arstechnica.com": rss(["Ars story", "Only one"], NOW - timedelta(minutes=1)),
            "feeds.bbci.co.uk/news/technology/rss.xml": httpx.Response(503),
        }
    )

    async def go():
        async with mock_http(router) as http:
            return await fetch_headlines(make_ctx(http=http), "tech", limit=5)

    items = run(go())
    assert [i["title"] for i in items] == ["Only one", "Ars story"]


def test_fetch_headlines_all_feeds_down():
    async def go():
        async with mock_http(lambda r: httpx.Response(500)) as http:
            return await fetch_headlines(make_ctx(http=http), "world")

    with pytest.raises(ToolError, match="unreachable"):
        run(go())


def test_fetch_headlines_query_retries_without_time_window():
    seen = []

    def handler(request):
        q = request.url.params["q"]
        seen.append(q)
        return rss([] if "when:3d" in q else ["Old SpaceX story"])

    async def go():
        async with mock_http(handler) as http:
            return await fetch_headlines(make_ctx(http=http), query="SpaceX launch")

    items = run(go())
    assert seen == ["SpaceX launch when:3d", "SpaceX launch"]
    assert [i["title"] for i in items] == ["Old SpaceX story"]


def test_fetch_headlines_does_not_mutate_cache():
    router = Router(
        {
            "news.google.com/rss": rss(["G1", "G2"]),
            "feeds.bbci.co.uk/news/rss.xml": rss(["B1"]),
            "feeds.npr.org/1001/rss.xml": rss(["N1"]),
        }
    )

    async def go():
        async with mock_http(router) as http:
            ctx = make_ctx(http=http)
            for _ in range(3):
                items = await fetch_headlines(ctx, "top", limit=10)
            return items

    items = run(go())
    assert len(items) == 4
    google_cached = next(v for k, v in news._CACHE.items() if "news.google.com" in k)[1]
    assert [i["title"] for i in google_cached] == ["G1", "G2"]
    assert len(router.requests) == 3  # later calls served from cache


def test_hacker_news():
    stories = {i: {"id": i, "title": f"HN {i}", "url": f"https://hn/{i}", "time": 1_700_000_000 + i, "score": i, "descendants": 1} for i in range(1, 20)}
    stories[3] = {"id": 3, "title": "Ask HN", "time": 1_700_000_000}

    def handler(request):
        if request.url.path.endswith("topstories.json"):
            return json_response(list(range(1, 20)))
        i = int(request.url.path.rsplit("/", 1)[1].split(".")[0])
        return json_response(stories[i])

    async def go():
        async with mock_http(handler) as http:
            return await fetch_headlines(make_ctx(http=http), "hackernews", limit=5)

    items = run(go())
    assert len(items) == 5
    assert items[2]["link"] == "https://news.ycombinator.com/item?id=3"
    assert items[0]["source"] == "Hacker News" and "points" in items[0]["summary"]


def test_get_news_tool_shape():
    router = Router({"news.google.com/rss/search": rss(["Federal Reserve news"])})

    async def go():
        async with mock_http(router) as http:
            return json.loads(await call_tool("get_news", {"query": "Federal Reserve", "limit": 3}, make_ctx(http=http)))

    out = run(go())
    assert out["query"] == "Federal Reserve" and out["category"] is None and out["count"] == 1
    art = out["articles"][0]
    assert set(art) == {"title", "source", "published", "age", "summary", "link"}
    assert art["age"] == "just now" and datetime.fromisoformat(art["published"])
