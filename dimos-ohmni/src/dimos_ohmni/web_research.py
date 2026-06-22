"""WebResearcher — agent-callable skills for online lookup.

Free / no-key research stack. Skills:
- `web_search(query, max_results)`  — DDG HTML, with optional searxng
                                      override via SEARXNG_URL env.
- `read_url(url, max_chars)`        — trafilatura-extracted article
                                      text, with regex fallback.
- `arxiv_search(query, max_results)` — peer-reviewed papers (free).
- `wiki_search(query, max_results)`  — Wikipedia summaries (free).
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from html import unescape

from dimos.agents.annotation import skill
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
)


def _http_get(url: str, timeout: float = 10.0, headers: dict | None = None) -> str:
    h = {"User-Agent": _DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _strip_html_basic(html: str) -> str:
    """Regex-based fallback when trafilatura isn't installed."""
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    html = re.sub(r"(?i)<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_html(html: str, url: str | None = None) -> str:
    """Best-available article extractor. Tries trafilatura (much
    cleaner output for blog/article pages) and falls back to regex."""
    try:
        import trafilatura  # type: ignore
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if text and text.strip():
            return text.strip()
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("trafilatura extract failed (%s); falling back", e)
    return _strip_html_basic(html)


# ---- web search backends ---------------------------------------------

def _search_searxng(query: str, max_results: int) -> list[dict]:
    """Hit a self-hosted or public searxng instance via JSON API.

    Set SEARXNG_URL to an instance's base URL (e.g.
    `https://searx.be`). Most public instances support `?format=json`.
    """
    base = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if not base:
        return []
    url = f"{base}/search?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
    try:
        body = _http_get(url, timeout=10.0)
        data = json.loads(body)
    except Exception as e:  # noqa: BLE001
        logger.warning("searxng search failed: %s", e)
        return []
    out: list[dict] = []
    for r in data.get("results", []):
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        })
        if len(out) >= max_results:
            break
    return out


def _search_ddg(query: str, max_results: int) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML results."""
    q = urllib.parse.quote_plus(query.strip())
    url = f"https://duckduckgo.com/html/?q={q}"
    try:
        html = _http_get(url, timeout=10.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("ddg search fetch failed: %s", e)
        return []
    block_re = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    out: list[dict] = []
    for m in block_re.finditer(html):
        href, title_html, snippet_html = m.groups()
        title = _strip_html_basic(title_html)
        snippet = _strip_html_basic(snippet_html)
        actual = href
        if "uddg=" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" in qs:
                actual = urllib.parse.unquote(qs["uddg"][0])
        out.append({"title": title, "url": actual, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


# ---- WebResearcher Module -------------------------------------------

class WebResearcher(Module):
    """Stateless agent skill container for online lookups (free tier)."""

    @rpc
    @skill
    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search the web with a short text query.

        Returns up to `max_results` items as `{title, url, snippet}`.
        Backend chain:
          1. searxng (if SEARXNG_URL is set) — most reliable, structured
          2. DuckDuckGo HTML scrape — no key, fragile to layout changes

        Use this when you need external context to plan an experiment —
        e.g. "RPLidar A2M8 motor PWM range" or "indoor SLAM voxel size".
        Keep queries terse (under 80 chars).
        """
        if not query or not query.strip():
            return []
        max_results = max(1, min(int(max_results), 10))
        results = _search_searxng(query, max_results)
        if results:
            return results
        return _search_ddg(query, max_results)

    @rpc
    @skill
    def read_url(self, url: str, max_chars: int = 8000) -> str:
        """Fetch a URL and return its readable plain-text content.

        Uses trafilatura (clean article extraction) when installed,
        falls back to regex stripping. Truncates to `max_chars` so
        the agent's context isn't blown out. Use after `web_search`
        to dig into a specific page.
        """
        if not url or not url.startswith(("http://", "https://")):
            return ""
        try:
            html = _http_get(url, timeout=15.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("read_url fetch failed: %s", e)
            return ""
        text = _strip_html(html, url=url)
        max_chars = max(500, min(int(max_chars), 50000))
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[truncated]"
        return text

    @rpc
    @skill
    def arxiv_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search arXiv for papers matching `query`.

        Free, structured, no key. Returns up to `max_results` items
        as `{title, url, summary, authors}`. Use for academic context
        on robotics, SLAM, perception, control.
        """
        if not query or not query.strip():
            return []
        max_results = max(1, min(int(max_results), 10))
        url = (
            "http://export.arxiv.org/api/query?"
            + urllib.parse.urlencode({
                "search_query": f"all:{query}",
                "max_results": str(max_results),
                "sortBy": "relevance",
            })
        )
        try:
            xml = _http_get(url, timeout=15.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("arxiv_search fetch failed: %s", e)
            return []
        out: list[dict] = []
        for m in re.finditer(r"(?s)<entry>(.*?)</entry>", xml):
            block = m.group(1)
            title = re.search(r"(?s)<title>(.*?)</title>", block)
            summary = re.search(r"(?s)<summary>(.*?)</summary>", block)
            link = re.search(r'<id>(.*?)</id>', block)
            authors = re.findall(r"(?s)<author>.*?<name>(.*?)</name>", block)
            out.append({
                "title": (title.group(1) if title else "").strip(),
                "url": (link.group(1) if link else "").strip(),
                "summary": (summary.group(1) if summary else "").strip()[:600],
                "authors": [a.strip() for a in authors[:4]],
            })
            if len(out) >= max_results:
                break
        return out

    @rpc
    @skill
    def wiki_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search Wikipedia for articles matching `query` and return
        page-summary entries. Free, structured, no key. Best for
        canonical definitions and well-established facts."""
        if not query or not query.strip():
            return []
        max_results = max(1, min(int(max_results), 10))
        # 1. opensearch endpoint to get titles
        url = (
            "https://en.wikipedia.org/w/api.php?"
            + urllib.parse.urlencode({
                "action": "opensearch",
                "search": query,
                "limit": str(max_results),
                "format": "json",
            })
        )
        try:
            body = _http_get(url, timeout=10.0)
            arr = json.loads(body)
        except Exception as e:  # noqa: BLE001
            logger.warning("wiki search failed: %s", e)
            return []
        if not isinstance(arr, list) or len(arr) < 4:
            return []
        titles, summaries, urls = arr[1], arr[2], arr[3]
        out: list[dict] = []
        for t, s, u in zip(titles, summaries, urls):
            out.append({"title": t, "url": u, "snippet": s[:400]})
        return out


web_researcher = WebResearcher.blueprint
