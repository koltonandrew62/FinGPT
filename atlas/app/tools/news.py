"""Live news from RSS/Atom feeds (Google News, BBC, NPR, CNBC, Ars) and Hacker News."""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from ..util import BROWSER_UA, clip, html_to_text, relative_time
from . import ToolError, tool

GOOGLE_TOPICS = {
    "world": "WORLD",
    "us": "NATION",
    "nation": "NATION",
    "business": "BUSINESS",
    "technology": "TECHNOLOGY",
    "science": "SCIENCE",
    "health": "HEALTH",
    "sports": "SPORTS",
    "entertainment": "ENTERTAINMENT",
}
CATEGORY_ALIASES = {"tech": "technology", "markets": "business", "finance": "business", "politics": "us"}

FALLBACK_FEEDS = {
    "top": ["https://feeds.bbci.co.uk/news/rss.xml", "https://feeds.npr.org/1001/rss.xml"],
    "world": ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://feeds.npr.org/1004/rss.xml"],
    "us": ["https://feeds.npr.org/1003/rss.xml", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"],
    "business": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],
    "technology": ["https://feeds.arstechnica.com/arstechnica/index", "https://feeds.bbci.co.uk/news/technology/rss.xml"],
    "science": ["https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"],
    "health": ["https://feeds.bbci.co.uk/news/health/rss.xml"],
    "sports": ["https://feeds.bbci.co.uk/sport/rss.xml"],
    "entertainment": ["https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"],
}
CATEGORIES = ["top", "world", "us", "business", "technology", "science", "health", "sports", "entertainment", "hackernews"]

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 300
_ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_feed(xml_text: str, default_source: str = "") -> list[dict]:
    """Parse RSS 2.0 or Atom into [{title, link, source, published, summary}]."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict] = []
    channel_title = ""
    if root.tag == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        if channel is not None:
            channel_title = _text(channel, "title")
        for item in root.iter("item"):
            title = html_to_text(_text(item, "title"))
            source_el = item.find("source")
            source = (source_el.text or "").strip() if source_el is not None and source_el.text else ""
            # Google News titles look like "Headline - Publisher"
            if source and title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")].strip()
            summary = html_to_text(_text(item, "description"))
            if summary and (summary.startswith(title[:40]) or len(summary) < 25):
                summary = ""
            items.append(
                {
                    "title": title,
                    "link": _text(item, "link"),
                    "source": source or default_source or _clean_channel(channel_title),
                    "published": _parse_date(_text(item, "pubDate")),
                    "summary": clip(summary, 280),
                }
            )
    elif root.tag == f"{_ATOM}feed":
        channel_title = _text(root, f"{_ATOM}title")
        for entry in root.iter(f"{_ATOM}entry"):
            link_el = entry.find(f"{_ATOM}link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary = html_to_text(_text(entry, f"{_ATOM}summary") or _text(entry, f"{_ATOM}content"))
            items.append(
                {
                    "title": html_to_text(_text(entry, f"{_ATOM}title")),
                    "link": link,
                    "source": default_source or _clean_channel(channel_title),
                    "published": _parse_date(_text(entry, f"{_ATOM}updated") or _text(entry, f"{_ATOM}published")),
                    "summary": clip(summary, 280),
                }
            )
    return [i for i in items if i["title"]]


def _clean_channel(title: str) -> str:
    title = re.sub(r"\s*[-|:]\s*(Top Stories|Home|News|World|Business|Technology).*$", "", title or "", flags=re.I)
    return title.strip() or "News"


async def _fetch_feed(http, url: str, source: str = "") -> list[dict]:
    cached = _CACHE.get(url)
    if cached and time.time() - cached[0] < _TTL:
        return cached[1]
    r = await http.get(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}, timeout=15)
    r.raise_for_status()
    items = parse_feed(r.text, source)
    _CACHE[url] = (time.time(), items)
    return items


def _google_url(settings, category: str | None = None, query: str | None = None) -> str:
    region, lang = settings.news_region, settings.news_language
    tail = f"hl={lang}-{region}&gl={region}&ceid={region}:{lang}"
    if query:
        return f"https://news.google.com/rss/search?q={quote_plus(query)}&{tail}"
    if category and category in GOOGLE_TOPICS:
        return f"https://news.google.com/rss/headlines/section/topic/{GOOGLE_TOPICS[category]}?{tail}"
    return f"https://news.google.com/rss?{tail}"


async def _hacker_news(http, limit: int) -> list[dict]:
    cached = _CACHE.get("hn")
    if cached and time.time() - cached[0] < _TTL:
        return cached[1][:limit]
    r = await http.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15)
    r.raise_for_status()
    ids = r.json()[: max(limit, 15)]

    async def one(i):
        try:
            resp = await http.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=15)
            return resp.json()
        except Exception:
            return None

    stories = await asyncio.gather(*(one(i) for i in ids))
    items = [
        {
            "title": s.get("title", ""),
            "link": s.get("url") or f"https://news.ycombinator.com/item?id={s.get('id')}",
            "source": "Hacker News",
            "published": datetime.fromtimestamp(s.get("time", 0), tz=timezone.utc),
            "summary": f"{s.get('score', 0)} points · {s.get('descendants', 0)} comments",
        }
        for s in stories
        if s and s.get("title")
    ]
    _CACHE["hn"] = (time.time(), items)
    return items[:limit]


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _sort(items: list[dict]) -> list[dict]:
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    return sorted(items, key=lambda i: i["published"] or epoch, reverse=True)


async def fetch_headlines(ctx, category: str = "top", query: str | None = None, limit: int = 10) -> list[dict]:
    category = CATEGORY_ALIASES.get((category or "top").lower(), (category or "top").lower())
    limit = max(1, min(int(limit or 10), 25))
    if category == "hackernews" and not query:
        return await _hacker_news(ctx.http, limit)

    items: list[dict] = []
    errors: list[str] = []
    if query:
        for q in (f"{query} when:3d", query):
            try:
                items = await _fetch_feed(ctx.http, _google_url(ctx.settings, query=q))
            except Exception as exc:
                errors.append(f"Google News: {exc.__class__.__name__}")
                break
            if items:
                break
    else:
        try:
            items = await _fetch_feed(ctx.http, _google_url(ctx.settings, category=category))
        except Exception as exc:
            errors.append(f"Google News: {exc.__class__.__name__}")
        if len(items) < limit:
            feeds = FALLBACK_FEEDS.get(category, FALLBACK_FEEDS["top"])
            results = await asyncio.gather(*(_fetch_feed(ctx.http, u) for u in feeds), return_exceptions=True)
            for url, res in zip(feeds, results):
                if isinstance(res, Exception):
                    errors.append(f"{url.split('/')[2]}: {res.__class__.__name__}")
                else:
                    items.extend(res)
    if not items and errors:
        raise ToolError("All news feeds are unreachable right now (" + "; ".join(errors[:3]) + ").")
    items = _dedupe(items) if query else _sort(_dedupe(items))
    if query:
        items = _sort(items)
    return items[:limit]


def _serialize(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        pub = it.get("published")
        out.append(
            {
                "title": it["title"],
                "source": it.get("source", ""),
                "published": pub.isoformat() if pub else None,
                "age": relative_time(pub),
                "summary": it.get("summary", ""),
                "link": it.get("link", ""),
            }
        )
    return out


@tool(
    "get_news",
    "Get the latest live news headlines. Use a category for general headlines, or a query to find "
    "current coverage of a specific topic, person, company, event or place. Returns titles, sources, "
    "ages and short summaries. For deeper detail on a story, follow up with web_search.",
    {
        "category": {"type": "string", "enum": CATEGORIES, "description": "Headline section. Default 'top'."},
        "query": {"type": "string", "description": "Optional topic search, e.g. 'Federal Reserve', 'SpaceX launch'."},
        "limit": {"type": "integer", "description": "Number of stories, 1-25. Default 10."},
    },
    label="Scanning news feeds",
)
async def get_news(ctx, category: str = "top", query: str | None = None, limit: int = 10):
    items = await fetch_headlines(ctx, category=category, query=query, limit=limit)
    return {
        "category": None if query else category,
        "query": query,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "count": len(items),
        "articles": _serialize(items),
        "note": "Links from Google News are redirect URLs; use web_search for article details.",
    }


async def headlines_for_panel(ctx, category: str) -> list[dict]:
    return _serialize(await fetch_headlines(ctx, category=category, limit=14))
