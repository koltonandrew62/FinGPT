"""Small shared helpers."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import unicodedata
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import httpx

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|nav|footer|header|form|iframe|aside)\b[^>]*>.*?</\1\s*>",
    re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|h[1-6]|tr|section|article|blockquote)\b[^>]*>", re.I)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(raw: str) -> str:
    text = _DROP_BLOCKS.sub(" ", raw)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clip_middle(text: str, limit: int) -> str:
    """Shorten text to about `limit` characters by cutting the middle, saying how much was left out. For approval
    cards: the end of a command, file or message is shown too, and the cut is never silent."""
    text = text or ""
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return f"{text[:head]}\n\n[… {len(text) - head - tail:,} more characters not shown …]\n\n{text[-tail:]}"


def relative_time(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Hostname dots in other scripts that browsers and IDNA treat as "." (after NFKC folding).
_IDNA_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


def is_public_ip(ip) -> bool:
    """True only for globally routable unicast addresses (no loopback/private/link-local/CGNAT/etc.).

    IPv6 forms that embed an IPv4 address (mapped, 6to4, Teredo, NAT64) are judged by that address too.
    """
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.split("%", 1)[0])  # drop an IPv6 zone id ("fe80::1%eth0")
        except ValueError:
            return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        embedded = ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not is_public_ip(embedded):
            return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    )


def _literal_ip(host: str):
    """The address a literal host stands for, or None for a real name."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0] if ":" in host else host)
    except ValueError:
        pass
    # The OS resolver also accepts legacy IPv4 spellings such as "127.1", "2130706433" or
    # "0x7f000001"; treat anything inet_aton() can parse as that address.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError, UnicodeError):
        return None


def _host_forms(hostname: str) -> set[str]:
    host = hostname.lower().rstrip(".")  # "localhost." resolves like "localhost"
    # Browsers percent-decode the host first ("%31%32%37.0.0.1" opens 127.0.0.1), then apply IDNA mapping,
    # so "127。0。0。1" or fullwidth digits reach 127.0.0.1 too.
    forms = {host}
    for h in (host, unquote(host).lower().rstrip(".")):
        forms.add(h)
        forms.add(unicodedata.normalize("NFKC", h).translate(_IDNA_DOTS).casefold().rstrip("."))
    return forms


def is_public_http_url(url: str) -> bool:
    """Reject non-http(s) schemes and literal local/private targets (no DNS lookup; see resolves_public)."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    for host in _host_forms(hostname):
        if not host or host == "localhost" or host.endswith(".local") or host.endswith(".localhost"):
            return False
        ip = _literal_ip(host)
        if ip is not None and not is_public_ip(ip):
            return False
    return True


class NonPublicAddress(ValueError):
    """The URL's host is, or resolves to, an address that isn't on the public internet."""


def _getaddrinfo(host: str, port: int) -> list[str]:
    """Every address the OS resolver returns for host (patched in tests)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


async def resolves_public(url: str) -> str:
    """Check a URL right before fetching it: http(s), and its host — as the HTTP client will spell it —
    is a public address or a name whose every DNS address is public (so "127.0.0.1.nip.io" fails).

    Returns the host. Raises NonPublicAddress for a local/private/non-web target, and OSError
    (socket.gaierror) when the name doesn't resolve. Run it again for every redirect hop.
    """
    if not is_public_http_url(url):
        raise NonPublicAddress(url)
    try:
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii")  # IDNA-encoded, exactly what the client connects to
    except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
        raise NonPublicAddress(url) from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise NonPublicAddress(url)
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith((".local", ".localhost")):
        raise NonPublicAddress(url)
    literal = _literal_ip(name)
    if literal is not None:
        if not is_public_ip(literal):
            raise NonPublicAddress(url)
        return host
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await asyncio.to_thread(_getaddrinfo, host, port)
    if not addresses or not all(is_public_ip(a) for a in addresses):
        raise NonPublicAddress(url)
    return host
