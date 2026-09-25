"""Isolated web worker: SSRF-safe fetching, readable-text extraction and search providers."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import httpx

from ..config import Settings
from ..security.ssrf import SSRFError, check_url, ip_is_public

log = logging.getLogger("nextai.web")


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table",
             "blockquote", "ul", "ol", "header", "footer", "nav"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.title = base, [], [], ""
        self._skip, self._in_title, self._href = 0, False, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self._href = urljoin(self.base, href)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.parts.append(data)
        if self._href and data.strip() and len(self.links) < 60:
            self.links.append((data.strip()[:80], self._href))


def html_to_text(body: str, base_url: str) -> tuple[str, str, list[tuple[str, str]]]:
    p = _TextExtractor(base_url)
    try:
        p.feed(body)
    except Exception:  # noqa: BLE001 - malformed HTML
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return p.title.strip(), text, p.links


def _charset(content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    return m.group(1) if m else "utf-8"


class WebClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        w = settings.web
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(w.timeout_seconds), follow_redirects=False, trust_env=w.use_system_proxy,
            headers={"User-Agent": w.user_agent, "Accept-Language": "ja,en;q=0.8"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5))
        self._provider_client = httpx.AsyncClient(timeout=httpx.Timeout(w.timeout_seconds), trust_env=True,
                                                  headers={"User-Agent": w.user_agent})
        self.peer_check = not w.use_system_proxy

    async def close(self) -> None:
        await self._client.aclose()
        await self._provider_client.aclose()

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        w = self.settings.web
        max_bytes = max_bytes or w.max_bytes
        for _ in range(w.max_redirects + 1):
            await check_url(url, list(w.allowed_ports))
            async with self._client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5"}) as r:
                if self.peer_check:
                    stream = r.extensions.get("network_stream")
                    addr = stream.get_extra_info("server_addr") if stream is not None else None
                    if addr and not ip_is_public(addr[0]):
                        raise SSRFError("接続先が内部アドレスでした (DNS rebinding をブロック)")
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                buf, truncated = bytearray(), False
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        truncated = True
                        del buf[max_bytes:]
                        break
                log.info("web fetch %s -> %s (%d bytes)", urlsplit(url).netloc, r.status_code, len(buf))
                return FetchResult(str(r.url), r.status_code, r.headers.get("content-type", ""), bytes(buf), truncated)
        raise SSRFError("リダイレクトが多すぎます")

    async def fetch_text(self, url: str, max_chars: int = 12000) -> dict:
        res = await self.fetch(url)
        ctype = res.content_type.lower()
        raw = res.body.decode(_charset(ctype), "replace")
        if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            title, text, links = html_to_text(raw, res.url)
        elif ctype.startswith(("text/", "application/json", "application/xml")):
            title, text, links = "", raw, []
        else:
            return {"url": res.url, "status": res.status, "title": "", "text": f"(非テキストコンテンツ: {ctype})", "links": []}
        return {"url": res.url, "status": res.status, "title": title[:200], "text": text[:max_chars],
                "truncated": res.truncated or len(text) > max_chars, "links": links[:20]}

    async def search(self, query: str, n: int = 6) -> list[dict]:
        w = self.settings.web
        query = query.strip()[:300]
        if not query:
            return []
        if w.search_provider == "searxng" and w.searxng_url:
            r = await self._provider_client.get(w.searxng_url.rstrip("/") + "/search",
                                                params={"q": query, "format": "json"})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])[:n]]
        if w.search_provider == "brave" and w.brave_api_key:
            r = await self._provider_client.get("https://api.search.brave.com/res/v1/web/search",
                                                params={"q": query, "count": n},
                                                headers={"X-Subscription-Token": w.brave_api_key, "Accept": "application/json"})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
                    for x in r.json().get("web", {}).get("results", [])[:n]]
        r = await self._provider_client.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=jp-jp")
        r.raise_for_status()
        return parse_duckduckgo(r.text)[:n]


def parse_duckduckgo(page: str) -> list[dict]:
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                         page, re.S):
        href, title, rest = m.group(1), m.group(2), m.group(3)
        href = html.unescape(href)
        if "uddg=" in href:
            href = parse_qs(urlsplit(href if href.startswith("http") else "https:" + href).query).get("uddg", [href])[0]
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()  # noqa: E731
        if href.startswith("http"):
            results.append({"title": clean(title), "url": href, "snippet": clean(sn.group(1) if sn else "")})
    return results
