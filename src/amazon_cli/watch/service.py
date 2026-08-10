"""Watchlist rules: alert hysteresis, the polite re-check loop, sparklines.

Everything here is deliberately free of terminal output so the interesting part
-- deciding whether a price move deserves to interrupt someone -- can be tested
as a table rather than by scraping stdout.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence

from amazon_cli import money
from amazon_cli.client.base import AmazonClient, validate_asin
from amazon_cli.client.product import get_product
from amazon_cli.client.types import ProductDetail
from amazon_cli.errors import AmzError, InputError
from amazon_cli.watch.store import PricePoint, Watched, WatchStore

#: Gap between two product fetches during ``watch check``, seconds. Randomised
#: because a metronome is exactly the signature bot detection looks for.
DELAY_RANGE: tuple[float, float] = (2.0, 6.0)

#: Floor the HTTP client enforces on its own, in case a caller loops faster.
MIN_INTERVAL: float = 2.0

_BLOCKS = "▁▂▃▄▅▆▇█"

# A target is a plain rupee amount, optionally with a currency prefix and at
# most two decimals. Stricter than `money.parse_paise` on purpose: that function
# scans a token out of scraped page text ("M.R.P.: Rs.34,990.00"), so it happily
# reads "1e9" as Rs.1. For something the user typed, trailing garbage is a typo
# worth reporting, not something to silently discard.
_TARGET_RE = re.compile(r"^\s*(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def normalize_asin(asin: str) -> str:
    """Uppercase and validate an ASIN, raising :class:`InputError`."""
    try:
        return validate_asin(asin)
    except ValueError as exc:
        raise InputError(
            f"{exc} -- an ASIN is 10 letters/digits, e.g. B0BZP2H373."
        ) from exc


def parse_target_paise(text: str) -> int:
    """Parse a user-supplied target price in **rupees** into paise.

    Rejects zero, negatives, non-numeric junk, and anything above Rs.1 crore
    (:data:`money.MAX_PAISE`), which is a fat-fingered extra digit rather than a
    price anyone is waiting for.
    """
    match = _TARGET_RE.match(str(text or ""))
    if match:
        paise = money.parse_paise(match.group(1))
        if paise > 0:
            return paise
    raise InputError(
        f"Invalid target price: {text!r}. Give a positive rupee amount "
        f"up to {money.format_inr(money.MAX_PAISE)}, e.g. 1999 or 2499.50."
    )


# ---------------------------------------------------------------------------
# the alert rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """Outcome of :func:`decide_alert`.

    ``notified_at_paise`` is the value to persist, *always* -- callers write it
    back unconditionally rather than only when ``alert`` is true, because the
    re-arm case changes the memory without alerting.
    """

    alert: bool
    notified_at_paise: int
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover -- convenience only
        return self.alert


def decide_alert(
    current_paise: int,
    target_paise: int,
    notified_at_paise: int,
    alerts_enabled: bool = True,
) -> Decision:
    """Decide whether this price deserves an alert. Pure; no I/O.

    The rule, ported from Bhav:

    ===================================  ==========================  ==========
    situation                            alert?                      memory
    ===================================  ==========================  ==========
    price above target                   no                          reset to 0
    at/below target, memory == 0         **yes** (first hit)         = price
    at/below target, price < memory      **yes** (dropped further)   = price
    at/below target, otherwise           no                          unchanged
    ===================================  ==========================  ==========

    The memory (``notified_at_paise``) is what stops a product parked at
    Rs.1,999 under a Rs.2,000 target from alerting on every single check, while
    still letting a further drop to Rs.1,799 through. It only ever moves *down*
    while the price is below target; it is reset to 0 -- re-armed -- the moment
    the price climbs back above.

    Muting suppresses the alert but **not** the re-arm. That asymmetry is
    deliberate: re-arming is a fact about the price, not about notifications. If
    mute froze the memory, a product that recovered above target and fell again
    during a quiet week would be silently swallowed the moment alerts came back
    on. Muting also never *advances* the memory -- a drop nobody was told about
    must still be tellable later.
    """
    notified_at_paise = int(notified_at_paise)

    # No usable price: the fetch may have succeeded but the page had no number.
    # Neither an alert nor a re-arm -- we know nothing about where the price is.
    if current_paise <= 0:
        return Decision(False, notified_at_paise, "no-price")

    # No target set: there is nothing to be below, so stay disarmed.
    if target_paise <= 0:
        return Decision(False, 0, "no-target")

    if current_paise > target_paise:
        return Decision(False, 0, "above-target")

    if not alerts_enabled:
        return Decision(False, notified_at_paise, "muted")

    if notified_at_paise == 0:
        return Decision(True, current_paise, "first-hit")

    if current_paise < notified_at_paise:
        return Decision(True, current_paise, "dropped-further")

    return Decision(False, notified_at_paise, "already-notified")


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """What happened to one product during a ``watch check``."""

    asin: str
    title: str = ""
    ok: bool = True
    alerted: bool = False
    reason: str = ""
    previous_paise: int = 0
    current_paise: int = 0
    target_paise: int = 0
    lowest_paise: int = 0
    availability: str = ""
    recorded: bool = False
    error: str = ""
    exit_code: int = 1

    @property
    def changed_paise(self) -> int:
        if not self.previous_paise or not self.current_paise:
            return 0
        return self.current_paise - self.previous_paise

    @property
    def change_percent(self) -> int:
        return money.change_percent(self.previous_paise, self.current_paise)

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "ok": self.ok,
            "alert": self.alerted,
            "reason": self.reason,
            "previous": money.rupees(self.previous_paise),
            "previous_paise": self.previous_paise,
            "price": money.rupees(self.current_paise),
            "price_paise": self.current_paise,
            "target": money.rupees(self.target_paise),
            "target_paise": self.target_paise,
            "lowest": money.rupees(self.lowest_paise),
            "lowest_paise": self.lowest_paise,
            "change_paise": self.changed_paise,
            "change_percent": self.change_percent,
            "availability": self.availability,
            "recorded": self.recorded,
            "error": self.error,
        }


async def _sleep(seconds: float) -> None:
    """Indirection so tests can make the polite gap free."""
    await asyncio.sleep(seconds)


def apply_check(
    store: WatchStore,
    entry: Watched,
    detail: ProductDetail,
    now: int | None = None,
) -> CheckResult:
    """Fold one successful fetch into the store and decide on an alert.

    Split out of :func:`check_all` so the whole state transition is testable
    without an event loop or a client.
    """
    now = int(time.time()) if now is None else int(now)
    price = int(detail.price or 0)

    updated = store.update_success(
        entry.asin,
        price_paise=price,
        mrp_paise=int(detail.mrp or 0),
        title=detail.title,
        brand=detail.brand,
        image_url=detail.image_url,
        rating=detail.rating,
        review_count=detail.review_count,
        availability=detail.availability,
        now=now,
    )
    recorded = store.append_price_point(entry.asin, price, now)

    decision = decide_alert(price, updated.target_paise, updated.notified_at_paise, updated.alerts_enabled)
    if decision.notified_at_paise != updated.notified_at_paise:
        store.set_notified(entry.asin, decision.notified_at_paise)

    return CheckResult(
        asin=entry.asin,
        title=updated.title or entry.title,
        ok=True,
        alerted=decision.alert,
        reason=decision.reason,
        previous_paise=entry.current_paise,
        # The price *this check* saw, not the one the row still remembers. The
        # store deliberately keeps the last known price when a page comes back
        # without one (see WatchStore.update_success), but reporting that as
        # this check's result would tell a `--json` consumer the product is
        # available at a price Amazon did not actually serve.
        current_paise=price,
        target_paise=updated.target_paise,
        lowest_paise=updated.lowest_paise,
        availability=updated.availability,
        recorded=recorded,
    )


async def check_all(
    store: WatchStore,
    only: Iterable[str] | None = None,
    *,
    client: AmazonClient | None = None,
    fetcher: Callable[[str], Awaitable[ProductDetail]] | None = None,
    delay_range: tuple[float, float] = DELAY_RANGE,
    now: int | None = None,
    rng: random.Random | None = None,
) -> list[CheckResult]:
    """Re-fetch every watched product, sequentially and politely.

    Sequential on purpose. A watchlist is a background chore, not something
    anyone is waiting on, and firing twenty parallel product-page requests at
    Amazon is the fastest way to earn a bot check that breaks every other
    command too. Between products we wait a random 2-6s (skipped after the
    last one, where it would only delay the exit).

    A failed fetch is recorded as ``last_error`` and leaves every price field
    untouched -- see :meth:`WatchStore.update_failure`.
    """
    entries = store.list_all()
    if only:
        wanted = {a.strip().upper() for a in only if a and a.strip()}
        entries = [e for e in entries if e.asin in wanted]

    if fetcher is None:
        if client is None:
            raise ValueError("check_all needs either a client or a fetcher")
        active = client

        async def fetcher(asin: str) -> ProductDetail:  # type: ignore[misc]
            return await get_product(active, asin)

    rng = rng or random
    results: list[CheckResult] = []

    for index, entry in enumerate(entries):
        stamp = int(time.time()) if now is None else int(now)
        try:
            detail = await fetcher(entry.asin)
        except Exception as exc:  # noqa: BLE001 -- one bad product must not end the run
            message = str(exc) or exc.__class__.__name__
            store.update_failure(entry.asin, message, stamp)
            results.append(
                CheckResult(
                    asin=entry.asin,
                    title=entry.title,
                    ok=False,
                    previous_paise=entry.current_paise,
                    current_paise=entry.current_paise,
                    target_paise=entry.target_paise,
                    lowest_paise=entry.lowest_paise,
                    reason="error",
                    error=message,
                    exit_code=exc.exit_code if isinstance(exc, AmzError) else 1,
                )
            )
        else:
            results.append(apply_check(store, entry, detail, stamp))

        if index < len(entries) - 1:
            low, high = delay_range
            if high > 0:
                await _sleep(rng.uniform(low, high))

    return results


async def seed_product(
    store: WatchStore,
    asin: str,
    *,
    client: AmazonClient | None = None,
    fetcher: Callable[[str], Awaitable[ProductDetail]] | None = None,
    now: int | None = None,
) -> CheckResult:
    """Fetch a freshly added product once so the row is not born empty.

    Uses the same fold as a regular check, which means adding a product already
    below its target alerts immediately -- which is what someone who just set
    that target wants to know.
    """
    entry = store.require(asin)
    if fetcher is None:
        if client is None:
            raise ValueError("seed_product needs either a client or a fetcher")
        active = client

        async def fetcher(target: str) -> ProductDetail:  # type: ignore[misc]
            return await get_product(active, target)

    stamp = int(time.time()) if now is None else int(now)
    try:
        detail = await fetcher(asin)
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or exc.__class__.__name__
        store.update_failure(asin, message, stamp)
        return CheckResult(
            asin=asin,
            title=entry.title,
            ok=False,
            target_paise=entry.target_paise,
            reason="error",
            error=message,
            exit_code=exc.exit_code if isinstance(exc, AmzError) else 1,
        )
    return apply_check(store, entry, detail, stamp)


# ---------------------------------------------------------------------------
# presentation helpers (pure)
# ---------------------------------------------------------------------------


def sparkline(values: Sequence[int], width: int = 0) -> str:
    """Render a price series as block characters.

    Degrades rather than crashes: an empty series is an empty string, and a flat
    series renders as a mid-height line instead of dividing by a zero range.
    Scaling is integer division on paise, so the same series always renders
    identically -- no float rounding wobble between runs.

    ``width`` > 0 evenly samples the series down to that many columns.
    """
    vals = [int(v) for v in values]
    if not vals:
        return ""
    if width and len(vals) > width:
        if width == 1:
            vals = [vals[-1]]
        else:
            last = len(vals) - 1
            vals = [vals[i * last // (width - 1)] for i in range(width)]

    low, high = min(vals), max(vals)
    if high == low:
        return _BLOCKS[len(_BLOCKS) // 2 - 1] * len(vals)

    span = high - low
    top = len(_BLOCKS) - 1
    return "".join(_BLOCKS[(v - low) * top // span] for v in vals)


def time_weighted_average(points: Sequence[PricePoint], now: int | None = None) -> int:
    """Average price weighted by how long it held, in paise.

    A plain mean over a transition log is meaningless -- a price that stood for
    one hour would count as much as one that stood for three weeks. Integer
    arithmetic throughout, so the result is an exact paise value.
    """
    if not points:
        return 0
    now = int(time.time()) if now is None else int(now)

    total_weight = 0
    accumulated = 0
    for index, point in enumerate(points):
        if index + 1 < len(points):
            end = points[index + 1].recorded_at
        else:
            end = max(now, point.recorded_at)
        weight = max(0, end - point.recorded_at)
        accumulated += point.paise * weight
        total_weight += weight

    if total_weight == 0:
        # Every point shares a timestamp (or there is only one): fall back to a
        # plain mean rather than reporting nothing.
        return sum(p.paise for p in points) // len(points)
    return accumulated // total_weight


#: Sort keys accepted by ``amz watch list``.
SORT_KEYS = ("added", "target", "price", "drop", "name")


def sort_entries(entries: Sequence[Watched], key: str = "added") -> list[Watched]:
    """Order a watchlist. Unknown prices always sort last, never as Rs.0."""
    items = list(entries)
    if key == "target":
        return sorted(items, key=lambda e: (e.target_paise, e.asin))
    if key == "price":
        return sorted(items, key=lambda e: (e.current_paise <= 0, e.current_paise, e.asin))
    if key == "drop":
        # Biggest fall from the highest price ever seen, first.
        return sorted(items, key=lambda e: (e.current_paise <= 0, e.drop_percent, e.asin))
    if key == "name":
        return sorted(items, key=lambda e: ((e.title or e.asin).lower(), e.asin))
    if key == "added":
        return sorted(items, key=lambda e: (e.added_at, e.asin))
    raise InputError(f"Unknown sort key {key!r}. Use one of: {', '.join(SORT_KEYS)}.")
