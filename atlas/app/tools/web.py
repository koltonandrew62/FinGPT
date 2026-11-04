"""Web research: live search (OpenRouter web plugin, DuckDuckGo fallback), page reader, Wikipedia."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, quote, urljoin, urlparse

from ..util import BROWSER_UA, clip, html_to_text, is_public_http_url
from . import ToolError, tool

MAX_REDIRECTS = 5


async def _openrouter_search(ctx, query: str, max_results: int) -> dict:
    model = await ctx.llm.resolve_search_model()
    today = datetime.now().astimezone().strftime("%A, %B %d, %Y")
    data = await ctx.llm.complete(
        [
            {
                "role": "system",
                "content": (
                    f"You are a research engine. Today is {today}. Using the web results provided, answer "
                    "the query with dense, current, verifiable facts: names, numbers, dates. Flag conflicting "
                    "reports. Prefer the most recent information. Keep it under 300 words."
                ),
            },
            {"role": "user", "content": query},
        ],
        model=model,
        plugins=[{"id": "web", "max_results": max_results}],
        max_tokens=900,
        temperature=0.2,
    )
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    sources, seen = [], set()
    for ann in msg.get("annotations") or []:
        cite = ann.get("url_citation") or {}
        url = cite.get("url")
        if url and url not in seen:
            seen.add(url)
            sources.append({"title": cite.get("title") or urlparse(url).netloc, "url": url})
    return {"query": query, "engine": f"openrouter-web ({model})", "answer": msg.get("content") or "", "sources": sources[:8]}


def _ddg_link(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if (not parsed.netloc or "duckduckgo.com" in parsed.netloc) and parsed.path.startswith("/l/"):
        # parse_qs already percent-decodes; decoding again would corrupt targets containing %XX.
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


def parse_ddg(html_text: str, limit: int) -> list[dict]:
    results = []
    blocks = re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', html_text)[1:]
    for block in blocks:
        a = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        snippet = re.search(r'<(a|div|td)\b[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</\1>', block, re.S)
        url = _ddg_link(a.group(1).replace("&amp;", "&"))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        results.append(
            {
                "title": html_to_text(a.group(2)),
                "url": url,
                "snippet": html_to_text(snippet.group(2)) if snippet else "",
            }
        )
        if len(results) >= limit:
            break
    return results


async def _ddg_search(ctx, query: str, limit: int) -> dict:
    r = await ctx.http.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": BROWSER_UA, "Referer": "https://html.duckduckgo.com/"},
        timeout=15,
    )
    r.raise_for_status()
    results = parse_ddg(r.text, limit)
    if not results:
        raise ToolError("Search returned no results.")
    return {"query": query, "engine": "duckduckgo", "results": results}


@tool(
    "web_search",
    "Search the live web and get a synthesized, cited answer. Use for current events, breaking news "
    "details, recent releases, sports results, facts after your training cutoff, or anything you are "
    "unsure about. Phrase the query like a precise search request.",
    {
        "query": {"type": "string", "description": "What to look up, e.g. 'latest jobs report US unemployment rate'."},
        "max_results": {"type": "integer", "description": "Pages to consult, 3-10. Default 6."},
    },
    ["query"],
    label="Searching the web",
    timeout=90,
)
async def web_search(ctx, query: str, max_results: int = 6):
    max_results = max(3, min(int(max_results or 6), 10))
    errors = []
    try:
        result = await _openrouter_search(ctx, query, max_results)
        if result["answer"]:
            return result
    except Exception as exc:
        errors.append(f"openrouter-web: {exc}")
    try:
        return await _ddg_search(ctx, query, max_results)
    except Exception as exc:
        errors.append(f"duckduckgo: {exc}")
    raise ToolError("Web search failed — " + " | ".join(errors))


@tool(
    "fetch_url",
    "Read the main text of a public web page or article (news story, docs, blog). "
    "Use when the user gives a link or you need the full contents of a specific source.",
    {"url": {"type": "string", "description": "Full http(s) URL."}},
    ["url"],
    label="Reading page",
)
async def fetch_url(ctx, url: str):
    if not is_public_http_url(url):
        raise ToolError("Only public http(s) URLs can be fetched.")
    # Follow redirects by hand so a public page can't bounce the request to localhost or the LAN.
    for _ in range(MAX_REDIRECTS + 1):
        r = await ctx.http.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=20,
            follow_redirects=False,
        )
        if not r.is_redirect:
            break
        url = urljoin(str(r.url), r.headers["location"])
        if not is_public_http_url(url):
            raise ToolError("That page redirects to a private or non-web address, so it can't be fetched.")
    else:
        raise ToolError("Too many redirects.")
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    body = r.text[:2_000_000]
    if "html" in ctype or body.lstrip().startswith("<"):
        title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        main = re.search(r"<(article|main)\b[^>]*>(.*?)</\1>", body, re.S | re.I)
        text = html_to_text(main.group(2) if main and len(main.group(2)) > 1500 else body)
        title = html_to_text(title_m.group(1)) if title_m else ""
    else:
        text, title = body, ""
    if "news.google.com" in str(r.url):
        raise ToolError("Google News redirect links can't be read directly; use web_search on the headline instead.")
    return {"url": str(r.url), "title": title, "content": clip(text, 9000)}


@tool(
    "wikipedia",
    "Get an encyclopedic summary of a person, place, organization, concept or historical event from Wikipedia.",
    {"query": {"type": "string"}},
    ["query"],
    label="Consulting Wikipedia",
)
async def wikipedia(ctx, query: str):
    s = await ctx.http.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "opensearch", "search": query, "limit": 3, "namespace": 0, "format": "json"},
        headers={"User-Agent": "ATLAS-assistant/1.0"},
        timeout=15,
    )
    s.raise_for_status()
    titles = s.json()[1]
    if not titles:
        raise ToolError(f"No Wikipedia article found for '{query}'.")
    r = await ctx.http.get(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(titles[0].replace(' ', '_'))}",
        headers={"User-Agent": "ATLAS-assistant/1.0"},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    return {
        "title": d.get("title"),
        "description": d.get("description"),
        "summary": d.get("extract"),
        "url": (d.get("content_urls") or {}).get("desktop", {}).get("page"),
        "other_matches": titles[1:],
    }
