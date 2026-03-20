"""Async HTTP client for Amazon.in."""

import httpx

BASE_URL = "https://www.amazon.in"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}


class AmazonClient:
    """Async HTTP client for Amazon.in with realistic browser headers."""

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=DEFAULT_HEADERS,
            timeout=self._timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc):
        if self._http:
            await self._http.aclose()

    async def fetch(self, path: str) -> str:
        """Fetch a page and return HTML text."""
        resp = await self._http.get(path)
        resp.raise_for_status()
        return resp.text
