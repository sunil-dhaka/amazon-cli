"""Async HTTP client for Amazon.in."""

import asyncio
import random
import re

import httpx

from amazon_cli.cache import ResponseCache
from amazon_cli.errors import (
    BotCheckError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
)

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
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# Markers of Amazon's "are you a robot" interstitial.
#
# Deliberately narrow: a false positive turns a perfectly good page into a hard
# error, so nothing that could plausibly appear in review prose belongs here.
# `bm-verify` is Akamai Bot Manager's challenge page, which is what Amazon.in
# actually serves when it throttles a search.
_BOT_MARKERS = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "/errors/validatecaptcha",
    "validatecaptcha",
    "api-services-support@amazon.com",
    "bm-verify",
    "to discuss automated access to amazon data",
)


def validate_asin(asin: str) -> str:
    """Validate and normalize an Amazon ASIN (10 alphanumeric chars)."""
    asin = (asin or "").strip().upper()
    if not _ASIN_RE.match(asin):
        raise ValueError(f"Invalid ASIN format: {asin!r}")
    return asin


def looks_like_bot_check(html: str) -> bool:
    """True when the body is a captcha / bot-verification interstitial.

    Amazon serves these with HTTP 200, so status alone cannot detect them. A
    challenge page is also tiny, which is the cheap signal we check first.
    """
    if not html:
        return False
    if len(html) < 8_000:
        lowered = html.lower()
        if any(marker in lowered for marker in _BOT_MARKERS):
            return True
        # A near-empty document with a meta-refresh is a challenge redirect.
        if "http-equiv=\"refresh\"" in lowered and "iframe" in lowered:
            return True
        return False
    lowered = html[:200_000].lower()
    return any(marker in lowered for marker in _BOT_MARKERS)


class AmazonClient:
    """Async HTTP client for Amazon.in with realistic browser headers.

    Adds three things a bare `httpx` call does not have: an opt-in on-disk
    cache, exponential backoff with jitter on throttling and transient network
    failures, and detection of the bot-check page Amazon serves with HTTP 200.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        cache: ResponseCache | None = None,
        max_retries: int = 3,
        min_interval: float = 0.0,
    ):
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._cache = cache or ResponseCache(0)
        self._max_retries = max(0, max_retries)
        # Floor on the gap between two requests from this client, so a batch
        # command cannot burst.
        self._min_interval = max(0.0, min_interval)
        self._last_request_at = 0.0
        self._pace_lock = asyncio.Lock()

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

    def _cache_key(self, path: str, params: dict | None) -> str:
        if not params:
            return path
        encoded = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{path}?{encoded}"

    async def _pace(self) -> None:
        """Hold the configured minimum gap between requests."""
        if self._min_interval <= 0:
            return
        async with self._pace_lock:
            loop = asyncio.get_running_loop()
            wait = self._min_interval - (loop.time() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = loop.time()

    async def fetch(self, path: str, params: dict | None = None) -> str:
        """Fetch a page and return HTML text.

        Raises :class:`NotFoundError`, :class:`BotCheckError`,
        :class:`RateLimitedError` or :class:`NetworkError` -- never a bare
        ``httpx`` exception.
        """
        key = self._cache_key(path, params)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                # Exponential backoff with full jitter. Amazon throttles by
                # burst rate, so a randomised wait recovers far better than a
                # fixed one when several requests are retrying together.
                delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                await asyncio.sleep(random.uniform(delay / 2, delay))

            await self._pace()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TimeoutException as exc:
                last_error = NetworkError(f"Request timed out after {self._timeout}s")
                last_error.__cause__ = exc
                continue
            except httpx.HTTPError as exc:
                last_error = NetworkError(f"Network error: {exc}")
                last_error.__cause__ = exc
                continue

            status = resp.status_code
            if status in (404, 410):
                # A missing page will still be missing on the next attempt.
                raise NotFoundError(f"Not found: {path}")
            if status in (429, 503):
                last_error = RateLimitedError(
                    f"Amazon is throttling this client (HTTP {status})"
                )
                continue
            if status >= 500:
                last_error = NetworkError(f"Amazon returned HTTP {status}")
                continue
            if status >= 400:
                raise NetworkError(f"Amazon returned HTTP {status}")

            html = resp.text
            if looks_like_bot_check(html):
                last_error = BotCheckError()
                continue

            self._cache.set(key, html)
            return html

        raise last_error or NetworkError(f"Could not fetch {path}")
