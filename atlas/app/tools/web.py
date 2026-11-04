"""Web research: live search (OpenRouter web plugin, DuckDuckGo fallback), page reader, Wikipedia."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

import httpx

from ..util import BROWSER_UA, NonPublicAddress, clip, html_to_text, is_public_http_url, resolves_public
from . import ToolError, tool

MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 2_000_000  # stop downloading a page after this much (decoded) content


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
    r, raw, truncated = await fetch_public(ctx.http, url)
    try:
        body = raw.decode(r.charset_encoding or "utf-8", errors="replace")
    except LookupError:  # a charset Python doesn't know
        body = raw.decode("utf-8", errors="replace")
    ctype = r.headers.get("content-type", "")
    if "html" in ctype or body.lstrip().startswith("<"):
        title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        main = re.search(r"<(article|main)\b[^>]*>(.*?)</\1>", body, re.S | re.I)
        text = html_to_text(main.group(2) if main and len(main.group(2)) > 1500 else body)
        title = html_to_text(title_m.group(1)) if title_m else ""
    else:
        text, title = body, ""
    if "news.google.com" in str(r.url):
        raise ToolError("Google News redirect links can't be read directly; use web_search on the headline instead.")
    out = {"url": str(r.url), "title": title, "content": clip(text, 9000)}
    if truncated:
        out["note"] = f"Only the first {MAX_PAGE_BYTES // 1_000_000} MB of this page was read."
    return out


async def fetch_public(http, url: str, headers: dict | None = None, timeout: float = 20,
                       max_bytes: int = MAX_PAGE_BYTES) -> tuple[httpx.Response, bytes, bool]:
    """GET a model-supplied URL without letting it reach this PC or the LAN.

    Every hop (the first request and each redirect, followed by hand up to MAX_REDIRECTS) must be http(s)
    with a host that is public and whose DNS addresses are all public. The body is streamed and cut off
    at max_bytes. Returns (response, body bytes, truncated); raises ToolError with a friendly message.
    """
    headers = headers or {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*"}
    for hop in range(MAX_REDIRECTS + 1):
        await _vet(url, redirected=hop > 0)
        async with http.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=False) as r:
            if r.is_redirect:
                try:
                    url = str(r.url.join(r.headers["location"]))
                except (httpx.InvalidURL, ValueError, UnicodeError):
                    raise ToolError("That page redirects to an invalid address, so it can't be fetched.") from None
                continue
            r.raise_for_status()
            chunks, size, truncated = [], 0, False
            async for chunk in r.aiter_bytes():
                if size + len(chunk) > max_bytes:
                    chunks.append(chunk[: max_bytes - size])
                    truncated = True
                    break
                chunks.append(chunk)
                size += len(chunk)
            return r, b"".join(chunks), truncated
    raise ToolError("Too many redirects.")


async def _vet(url: str, redirected: bool) -> None:
    try:
        await resolves_public(url)
    except NonPublicAddress:
        if redirected:
            raise ToolError("That page redirects to a private or non-web address, so it can't be fetched.") from None
        raise ToolError(
            "That address points to this computer or a private network, so it can't be fetched."
        ) from None
    except OSError:  # socket.gaierror: the name doesn't exist (or there's no connection)
        host = urlparse(url).hostname or url
        raise ToolError(f"Couldn't find the site {host} — check the address (or the internet connection).") from None


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
