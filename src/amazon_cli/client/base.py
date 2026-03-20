"""Async HTTP client for Amazon.in."""

import re

import httpx

BASE_URL = "https://www.amazon.in"

# Amazon requires a realistic User-Agent or it returns bot-check pages.
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

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def validate_asin(asin: str) -> str:
    """Validate and normalize an Amazon ASIN (10 alphanumeric chars)."""
    asin = asin.strip().upper()
    if not _ASIN_RE.match(asin):
        raise ValueError(f"Invalid ASIN format: {asin!r}")
    return asin


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

    async def fetch(self, path: str, params: dict | None = None) -> str:
        """Fetch a page and return HTML text."""
        try:
            resp = await self._http.get(path, params=params)
        except httpx.ReadTimeout:
            raise TimeoutError(f"Request timed out") from None
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"HTTP {exc.response.status_code}",
                request=exc.request,
                response=exc.response,
            ) from None
        return resp.text
