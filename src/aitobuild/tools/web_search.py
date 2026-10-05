"""Shared web search adapters (cheap API/snippet default)."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from typing import Protocol
from urllib.error import URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen
import re


@dataclass(slots=True, frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class WebSearchAdapter(Protocol):
    def search(self, *, query: str, max_results: int = 5) -> tuple[WebSearchResult, ...]: ...


@dataclass
class MockWebSearchAdapter:
    results: list[WebSearchResult] = field(default_factory=list)

    def search(self, *, query: str, max_results: int = 5) -> tuple[WebSearchResult, ...]:
        needle = query.strip().lower()
        if not needle:
            raise ValueError("query must be non-empty")
        bound = max(1, min(max_results, 10))
        if self.results:
            matched = [item for item in self.results if needle in item.title.lower() or needle in item.snippet.lower()]
            selected = matched or self.results
            return tuple(selected[:bound])
        return tuple(
            WebSearchResult(
                title=f"Mock result for {query.strip()}",
                url=f"https://example.invalid/search?q={quote_plus(query.strip())}",
                snippet=f"Snippet describing {query.strip()} (mock web_search).",
            )
            for _ in range(min(bound, 3))
        )


class DuckDuckGoLiteSearchAdapter:
    """Lightweight HTML snippet search via DuckDuckGo Lite (no browser)."""

    def search(self, *, query: str, max_results: int = 5) -> tuple[WebSearchResult, ...]:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("query must be non-empty")
        bound = max(1, min(max_results, 10))
        url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(cleaned)}"
        request = Request(url, headers={"User-Agent": "aitobuild-web-search/0.1"})
        try:
            with urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", errors="replace")
        except URLError as exc:
            raise RuntimeError(f"web_search request failed: {exc}") from exc
        return _parse_duckduckgo_lite(html, max_results=bound)


def build_web_search_adapter(*, mode: str = "mock") -> WebSearchAdapter:
    normalized = mode.strip().lower()
    if normalized in {"", "mock"}:
        return MockWebSearchAdapter()
    if normalized in {"duckduckgo", "ddg", "live"}:
        return DuckDuckGoLiteSearchAdapter()
    raise ValueError("AITOBUILD_WEB_SEARCH_ADAPTER must be mock or duckduckgo")


def _parse_duckduckgo_lite(html: str, *, max_results: int) -> tuple[WebSearchResult, ...]:
    pattern = re.compile(
        r'class="result-link"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'.*?class="result-snippet"[^>]*>(?P<snippet>.*?)</td>',
        re.IGNORECASE | re.DOTALL,
    )
    results: list[WebSearchResult] = []
    for match in pattern.finditer(html):
        title = _strip_tags(match.group("title"))
        snippet = _strip_tags(match.group("snippet"))
        href = unescape(match.group("href"))
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://duckduckgo.com", href)
        results.append(WebSearchResult(title=title, url=href, snippet=snippet))
        if len(results) >= max_results:
            break
    return tuple(results)


def _strip_tags(value: str) -> str:
    without = re.sub(r"<[^>]+>", " ", value)
    return unescape(re.sub(r"\s+", " ", without)).strip()
