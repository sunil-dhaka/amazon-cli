"""Rules tests: alert hysteresis, the polite check loop, the pure helpers.

The centrepiece is a set of 720-check (one month, hourly) simulations over
scripted price paths. Each asserts the *exact* number of alerts and the *exact*
prices alerted at -- an off-by-one in the hysteresis is the difference between
a useful watchlist and one that pages you every hour until you delete it.
"""

import random

import pytest

from amazon_cli.client.types import ProductDetail
from amazon_cli.errors import (
    BotCheckError,
    InputError,
    NetworkError,
    NotFoundError,
    ParseError,
    RateLimitedError,
)
from amazon_cli.watch import service
from amazon_cli.watch.service import (
    CheckResult,
    Decision,
    apply_check,
    check_all,
    decide_alert,
    normalize_asin,
    parse_target_paise,
    seed_product,
    sort_entries,
    sparkline,
    time_weighted_average,
)
from amazon_cli.watch.store import PricePoint, Watched, WatchStore

ASIN = "B0BZP2H373"
OTHER = "B0C3ZYFZ77"
THIRD = "1847941834"

TARGET = 2_000_00
ABOVE = 2_100_00
AT = 2_000_00
BELOW = 1_900_00


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AMZ_WATCH_DB", str(tmp_path / "watch.db"))
    with WatchStore() as s:
        yield s


#: Captured before any fixture patches it out.
_REAL_SLEEP = service._sleep


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """No test in this module may ever wait on a wall clock."""
    calls = []

    async def fake(seconds):
        calls.append(seconds)

    monkeypatch.setattr(service, "_sleep", fake)
    return calls


def detail(asin=ASIN, price=0, **kw):
    kw.setdefault("title", "Sony WH-1000XM5")
    kw.setdefault("brand", "Sony")
    return ProductDetail(asin=asin, price=price, **kw)


# ===========================================================================
# the alert rule, exhaustively
# ===========================================================================


def _memories(price):
    """The four interesting ``notified_at_paise`` values around a price."""
    return {
        "zero": 0,
        "equal": price,
        "higher": price + 50_00,
        "lower": price - 50_00,
    }


def _expected(price, memory_kind, memory, muted):
    if price > TARGET:
        return Decision(False, 0, "above-target")
    if muted:
        return Decision(False, memory, "muted")
    if memory == 0:
        return Decision(True, price, "first-hit")
    if price < memory:
        return Decision(True, price, "dropped-further")
    return Decision(False, memory, "already-notified")


TABLE = [
    pytest.param(price, kind, memory, muted, id=f"{label}-{kind}-{'muted' if muted else 'live'}")
    for price, label in ((ABOVE, "above"), (AT, "at"), (BELOW, "below"))
    for kind, memory in _memories(price).items()
    for muted in (False, True)
]


@pytest.mark.parametrize("price, memory_kind, memory, muted", TABLE)
def test_decide_alert_table(price, memory_kind, memory, muted):
    """3 price positions x 4 memory positions x muted/unmuted = 24 rows."""
    got = decide_alert(price, TARGET, memory, alerts_enabled=not muted)
    assert got == _expected(price, memory_kind, memory, muted)


def test_the_table_really_covers_twenty_four_rows():
    assert len(TABLE) == 24


def test_exactly_at_target_counts_as_a_hit():
    assert decide_alert(TARGET, TARGET, 0) == Decision(True, TARGET, "first-hit")


def test_one_paise_above_target_does_not():
    assert decide_alert(TARGET + 1, TARGET, 0) == Decision(False, 0, "above-target")


def test_one_paise_below_the_memory_alerts():
    assert decide_alert(BELOW, TARGET, BELOW + 1) == Decision(True, BELOW, "dropped-further")


@pytest.mark.parametrize("price", [0, -1, -1_00])
@pytest.mark.parametrize("memory", [0, 1_900_00])
@pytest.mark.parametrize("muted", [False, True])
def test_no_price_neither_alerts_nor_rearms(price, memory, muted):
    assert decide_alert(price, TARGET, memory, alerts_enabled=not muted) == Decision(
        False, memory, "no-price"
    )


@pytest.mark.parametrize("target", [0, -1])
def test_no_target_stays_disarmed(target):
    assert decide_alert(1_00, target, 5_00) == Decision(False, 0, "no-target")


def test_memory_is_coerced_to_int():
    got = decide_alert(BELOW, TARGET, "0")
    assert got.alert is True
    assert got.notified_at_paise == BELOW


def test_decision_is_falsy_when_it_does_not_alert():
    assert not decide_alert(ABOVE, TARGET, 0)
    assert decide_alert(BELOW, TARGET, 0)


# ---------------------------------------------------------------------------
# muting must never swallow an alert
# ---------------------------------------------------------------------------


def test_muted_price_above_target_still_rearms():
    got = decide_alert(ABOVE, TARGET, 1_950_00, alerts_enabled=False)
    assert got == Decision(False, 0, "above-target")


def test_muting_never_advances_the_memory():
    """Every muted evaluation must hand back the memory it was given."""
    for price in (AT, BELOW, BELOW - 100_00, BELOW + 50_00):
        for memory in (0, 1_950_00, 1_800_00, 2_000_00):
            got = decide_alert(price, TARGET, memory, alerts_enabled=False)
            assert got.notified_at_paise == memory
            assert got.alert is False


def test_unmuting_at_an_untold_price_alerts(store):
    """The Bhav Android bug: a drop that happened while muted vanished forever."""
    store.add(ASIN, target_paise=TARGET, now=0)

    apply_check(store, store.require(ASIN), detail(price=2_500_00), now=1)
    assert store.require(ASIN).notified_at_paise == 0

    first = apply_check(store, store.require(ASIN), detail(price=BELOW), now=2)
    assert first.alerted is True
    assert store.require(ASIN).notified_at_paise == BELOW

    store.set_alerts(ASIN, False)

    # Recovers above target while muted -- the trigger must still re-arm.
    apply_check(store, store.require(ASIN), detail(price=2_600_00), now=3)
    assert store.require(ASIN).notified_at_paise == 0

    # Falls again while muted -- silent, and the memory must not advance.
    quiet = apply_check(store, store.require(ASIN), detail(price=1_800_00), now=4)
    assert quiet.alerted is False
    assert quiet.reason == "muted"
    assert store.require(ASIN).notified_at_paise == 0

    store.set_alerts(ASIN, True)

    loud = apply_check(store, store.require(ASIN), detail(price=1_800_00), now=5)
    assert loud.alerted is True, "a drop nobody was told about must still be tellable"
    assert loud.reason == "first-hit"
    assert store.require(ASIN).notified_at_paise == 1_800_00


def test_unmuting_below_a_remembered_price_alerts(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    apply_check(store, store.require(ASIN), detail(price=1_900_00), now=1)
    store.set_alerts(ASIN, False)

    apply_check(store, store.require(ASIN), detail(price=1_700_00), now=2)
    assert store.require(ASIN).notified_at_paise == 1_900_00

    store.set_alerts(ASIN, True)
    result = apply_check(store, store.require(ASIN), detail(price=1_700_00), now=3)
    assert result.alerted is True
    assert result.reason == "dropped-further"


def test_muted_run_never_alerts_at_all(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    store.set_alerts(ASIN, False)
    prices = [2_500_00, 1_900_00, 1_800_00, 2_500_00, 1_700_00, 1_700_00]
    alerts = [
        apply_check(store, store.require(ASIN), detail(price=p), now=i).alerted
        for i, p in enumerate(prices)
    ]
    assert alerts == [False] * len(prices)


# ===========================================================================
# a simulated month: 720 hourly checks over a scripted price path
# ===========================================================================


def run_month(store, asin, prices, *, target):
    """Fold a scripted price path through the real check machinery.

    Returns ``(alerted_prices, results)``. One check per hour, 720 hours.
    """
    store.add(asin, target_paise=target, now=0)
    alerted: list[int] = []
    results: list[CheckResult] = []
    for hour, price in enumerate(prices):
        result = apply_check(
            store, store.require(asin), detail(asin=asin, price=price), now=3_600 * (hour + 1)
        )
        results.append(result)
        if result.alerted:
            alerted.append(result.current_paise)
    return alerted, results


HOURS = 720


def test_month_parked_below_target_alerts_exactly_once(store):
    alerted, _ = run_month(store, ASIN, [1_999_00] * HOURS, target=2_000_00)
    assert alerted == [1_999_00]
    assert len(store.history(ASIN)) == 1


def test_month_steady_dip_and_recovery_alerts_once(store):
    prices = [3_000_00] * 300 + [2_400_00] * 200 + [3_000_00] * 220
    assert len(prices) == HOURS
    alerted, results = run_month(store, ASIN, prices, target=2_500_00)
    assert alerted == [2_400_00]
    assert [p.paise for p in store.history(ASIN)] == [3_000_00, 2_400_00, 3_000_00]
    assert sum(r.recorded for r in results) == 3


def test_month_oscillator_crossing_daily_alerts_once_per_crossing(store):
    """Below target every morning, above it every afternoon: 30 real events."""
    prices = [2_400_00 if hour % 24 < 12 else 2_600_00 for hour in range(HOURS)]
    alerted, _ = run_month(store, ASIN, prices, target=2_500_00)
    assert alerted == [2_400_00] * 30
    assert len(store.history(ASIN)) == 60


def test_month_downward_ratchet_alerts_on_each_new_low(store):
    steps = [3_000_00 - 40_00 * k for k in range(24)]
    prices = [p for p in steps for _ in range(30)]
    assert len(prices) == HOURS
    alerted, _ = run_month(store, ASIN, prices, target=2_500_00)
    assert alerted == [p for p in steps if p <= 2_500_00]
    assert alerted == [
        2_480_00, 2_440_00, 2_400_00, 2_360_00, 2_320_00, 2_280_00,
        2_240_00, 2_200_00, 2_160_00, 2_120_00, 2_080_00,
    ]
    assert len(alerted) == 11
    assert len(store.history(ASIN)) == 24
    assert store.require(ASIN).lowest_paise == 2_080_00


def test_month_never_cheap_never_alerts(store):
    rng = random.Random(4242)
    prices = [rng.randrange(2_700_00, 3_200_01, 1_00) for _ in range(HOURS)]
    assert min(prices) > 2_500_00
    alerted, _ = run_month(store, ASIN, prices, target=2_500_00)
    assert alerted == []
    assert store.require(ASIN).notified_at_paise == 0

    expected_rows = 1 + sum(1 for a, b in zip(prices, prices[1:]) if a != b)
    assert len(store.history(ASIN)) == expected_rows


def test_month_with_an_unavailable_window_keeps_its_memory(store):
    prices = [2_400_00] * 100 + [0] * 100 + [2_400_00] * 100 + [2_300_00] * 420
    assert len(prices) == HOURS
    alerted, results = run_month(store, ASIN, prices, target=2_500_00)
    assert alerted == [2_400_00, 2_300_00]
    assert [p.paise for p in store.history(ASIN)] == [2_400_00, 2_300_00]

    # Nothing during the blackout recorded, alerted, or re-armed.
    blackout = results[100:200]
    assert all(r.reason == "no-price" for r in blackout)
    assert not any(r.recorded for r in blackout)
    entry = store.require(ASIN)
    assert entry.lowest_paise == 2_300_00
    assert entry.highest_paise == 2_400_00


def test_month_sawtooth_under_target_does_not_alert_per_dip(store):
    """+/-1 rupee wobble under the target: two real events, not 360."""
    prices = [1_999_00 if hour % 2 == 0 else 1_998_00 for hour in range(HOURS)]
    alerted, _ = run_month(store, ASIN, prices, target=2_000_00)
    assert alerted == [1_999_00, 1_998_00]
    assert len(store.history(ASIN)) == HOURS, "every wobble is still history"


def test_month_seeded_walk_extremes_are_exact(store):
    rng = random.Random(20260810)
    price = 2_500_00
    prices = []
    for _ in range(HOURS):
        price = max(1_00, price + rng.randrange(-30_00, 30_01, 1_00))
        prices.append(price)

    alerted, _ = run_month(store, ASIN, prices, target=2_400_00)
    entry = store.require(ASIN)
    assert entry.lowest_paise == min(prices)
    assert entry.highest_paise == max(prices)
    assert entry.current_paise == prices[-1]
    assert isinstance(entry.lowest_paise, int) and isinstance(entry.highest_paise, int)

    # Independently recompute the alert stream from the pure rule.
    memory = 0
    expected = []
    for price in prices:
        decision = decide_alert(price, 2_400_00, memory)
        memory = decision.notified_at_paise
        if decision.alert:
            expected.append(price)
    assert alerted == expected
    assert alerted, "the walk must actually cross the target"


# ===========================================================================
# apply_check state transitions
# ===========================================================================


def test_apply_check_folds_metadata_and_reports_the_delta(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    apply_check(store, store.require(ASIN), detail(price=2_500_00), now=10)
    result = apply_check(
        store,
        store.require(ASIN),
        detail(price=2_000_00, mrp=3_000_00, availability="In stock", rating=4.5, review_count=7),
        now=20,
    )
    assert result.ok is True
    assert result.previous_paise == 2_500_00
    assert result.current_paise == 2_000_00
    assert result.changed_paise == -500_00
    assert result.change_percent == -20
    assert result.target_paise == TARGET
    assert result.lowest_paise == 2_000_00
    assert result.availability == "In stock"
    assert result.recorded is True
    assert result.alerted is True
    assert result.reason == "first-hit"


def test_apply_check_with_no_price_changes_nothing_and_clears_the_error(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    apply_check(store, store.require(ASIN), detail(price=1_900_00), now=10)
    store.update_failure(ASIN, "earlier boom", now=11)
    before = store.require(ASIN)

    result = apply_check(
        store, store.require(ASIN), detail(price=0, availability="Currently unavailable"), now=12
    )
    after = store.require(ASIN)
    assert result.alerted is False
    assert result.reason == "no-price"
    assert result.recorded is False
    assert after.last_error == ""
    assert after.current_paise == before.current_paise
    assert after.lowest_paise == before.lowest_paise
    assert after.highest_paise == before.highest_paise
    assert after.notified_at_paise == before.notified_at_paise
    assert len(store.history(ASIN)) == 1


def test_a_no_price_check_reports_no_price_not_the_remembered_one(store):
    """The row keeps the last known price; *this check* still saw nothing.

    Reporting the remembered price here would tell a `--json` consumer the
    product is on sale at a price Amazon never served, and would make the
    CLI's "no price / unavailable" line unreachable for anything that had
    ever had a price.
    """
    store.add(ASIN, target_paise=TARGET, now=0)
    apply_check(store, store.require(ASIN), detail(price=1_900_00), now=10)

    result = apply_check(
        store, store.require(ASIN), detail(price=0, availability="Currently unavailable"), now=11
    )
    assert result.current_paise == 0
    assert result.previous_paise == 1_900_00
    assert result.changed_paise == 0
    assert result.change_percent == 0
    assert result.to_dict()["price_paise"] == 0
    assert result.availability == "Currently unavailable"
    # ...while the stored history is untouched.
    assert store.require(ASIN).current_paise == 1_900_00
    assert result.lowest_paise == 1_900_00


def test_apply_check_result_to_dict_has_paise(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    result = apply_check(store, store.require(ASIN), detail(price=1_999_50), now=1)
    data = result.to_dict()
    assert data["price_paise"] == 1_999_50
    assert data["price"] == 1999.5
    assert data["target_paise"] == TARGET
    assert data["alert"] is True
    assert data["reason"] == "first-hit"


def test_check_result_deltas_are_zero_without_both_prices():
    assert CheckResult(ASIN, previous_paise=0, current_paise=1_00).changed_paise == 0
    assert CheckResult(ASIN, previous_paise=1_00, current_paise=0).changed_paise == 0
    assert CheckResult(ASIN, previous_paise=0, current_paise=0).change_percent == 0


# ===========================================================================
# check_all: politeness, resilience, filtering
# ===========================================================================


def _fill(store, *asins, target=TARGET):
    for i, asin in enumerate(asins):
        store.add(asin, target_paise=target, now=i)


async def test_check_all_visits_products_sequentially(store, no_real_sleep):
    _fill(store, ASIN, OTHER, THIRD)
    seen = []

    async def fetcher(asin):
        seen.append(asin)
        return detail(asin=asin, price=2_500_00)

    results = await check_all(store, fetcher=fetcher, rng=random.Random(7), now=100)
    assert seen == [ASIN, OTHER, THIRD]
    assert [r.asin for r in results] == [ASIN, OTHER, THIRD]


async def test_check_all_waits_between_products_but_not_after_the_last(store, no_real_sleep):
    _fill(store, ASIN, OTHER, THIRD)

    async def fetcher(asin):
        return detail(asin=asin, price=2_500_00)

    await check_all(store, fetcher=fetcher, delay_range=(2.0, 6.0), rng=random.Random(7), now=1)
    assert len(no_real_sleep) == 2, "n-1 gaps for n products"
    assert all(2.0 <= gap <= 6.0 for gap in no_real_sleep)
    assert len(set(no_real_sleep)) > 1, "a metronome is the signature bots are caught by"


async def test_check_all_makes_no_gap_for_a_single_product(store, no_real_sleep):
    _fill(store, ASIN)

    async def fetcher(asin):
        return detail(asin=asin, price=2_500_00)

    await check_all(store, fetcher=fetcher, rng=random.Random(7), now=1)
    assert no_real_sleep == []


async def test_check_all_skips_the_gap_when_the_range_is_zero(store, no_real_sleep):
    _fill(store, ASIN, OTHER)

    async def fetcher(asin):
        return detail(asin=asin, price=2_500_00)

    await check_all(store, fetcher=fetcher, delay_range=(0.0, 0.0), now=1)
    assert no_real_sleep == []


@pytest.mark.parametrize(
    "exc, code",
    [
        (NetworkError("connection reset"), 3),
        (NotFoundError("no such product"), 4),
        (BotCheckError(), 5),
        (RateLimitedError("429 from Amazon"), 5),
        (ParseError("markup changed"), 6),
        (InputError("bad asin"), 2),
        (RuntimeError("something else entirely"), 1),
    ],
)
async def test_one_failure_does_not_abort_the_sweep(store, exc, code):
    _fill(store, ASIN, OTHER, THIRD, target=2_500_00)
    for asin in (ASIN, OTHER, THIRD):
        store.update_success(asin, price_paise=2_500_00, mrp_paise=3_000_00, now=5)
        store.append_price_point(asin, 2_500_00, 5)
        store.set_notified(asin, 2_450_00)
    before = store.require(OTHER)

    async def fetcher(asin):
        if asin == OTHER:
            raise exc
        return detail(asin=asin, price=2_400_00)

    results = await check_all(store, fetcher=fetcher, delay_range=(0.0, 0.0), now=50)
    assert [r.ok for r in results] == [True, False, True]

    failed = results[1]
    assert failed.exit_code == code
    assert failed.reason == "error"
    assert failed.error == (str(exc) or exc.__class__.__name__)

    after = store.require(OTHER)
    assert after.last_error == failed.error
    assert after.last_checked_at == 50
    assert after.current_paise == before.current_paise
    assert after.lowest_paise == before.lowest_paise
    assert after.highest_paise == before.highest_paise
    assert after.notified_at_paise == before.notified_at_paise
    assert after.last_success_at == before.last_success_at
    assert len(store.history(OTHER)) == 1

    # The other two carried on and alerted.
    assert results[0].alerted and results[2].alerted


async def test_a_failure_with_an_empty_message_still_names_the_error(store):
    _fill(store, ASIN)

    async def fetcher(asin):
        raise NetworkError("")

    results = await check_all(store, fetcher=fetcher, delay_range=(0.0, 0.0), now=1)
    assert results[0].error == "NetworkError"
    assert store.require(ASIN).last_error == "NetworkError"


async def test_check_all_only_filter(store):
    _fill(store, ASIN, OTHER, THIRD)
    seen = []

    async def fetcher(asin):
        seen.append(asin)
        return detail(asin=asin, price=2_500_00)

    results = await check_all(
        store, only=[OTHER.lower(), "  ", ""], fetcher=fetcher, delay_range=(0.0, 0.0), now=1
    )
    assert seen == [OTHER]
    assert [r.asin for r in results] == [OTHER]


async def test_check_all_on_an_empty_watchlist_is_empty(store):
    async def fetcher(asin):  # pragma: no cover -- must never run
        raise AssertionError("nothing to fetch")

    assert await check_all(store, fetcher=fetcher) == []


async def test_the_sleep_indirection_really_awaits(store):
    """The seam tests patch must genuinely be an awaitable delay."""
    assert await _REAL_SLEEP(0) is None


async def test_check_all_needs_a_client_or_a_fetcher(store):
    _fill(store, ASIN)
    with pytest.raises(ValueError):
        await check_all(store)


async def test_check_all_recovers_a_product_after_a_failure(store):
    _fill(store, ASIN)
    calls = {"n": 0}

    async def fetcher(asin):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NetworkError("flaky")
        return detail(asin=asin, price=1_900_00)

    first = await check_all(store, fetcher=fetcher, delay_range=(0.0, 0.0), now=1)
    assert first[0].ok is False
    assert store.require(ASIN).last_error == "flaky"

    second = await check_all(store, fetcher=fetcher, delay_range=(0.0, 0.0), now=2)
    assert second[0].ok is True
    assert second[0].alerted is True
    assert store.require(ASIN).last_error == ""


# ===========================================================================
# seed_product
# ===========================================================================


async def test_seed_product_alerts_when_already_below_target(store):
    store.add(ASIN, target_paise=TARGET, now=0)

    async def fetcher(asin):
        return detail(asin=asin, price=1_500_00, mrp=2_500_00)

    result = await seed_product(store, ASIN, fetcher=fetcher, now=9)
    assert result.ok is True
    assert result.alerted is True
    assert result.reason == "first-hit"
    entry = store.require(ASIN)
    assert entry.current_paise == 1_500_00
    assert entry.notified_at_paise == 1_500_00
    assert [p.paise for p in store.history(ASIN)] == [1_500_00]


async def test_seed_product_records_a_failure_without_prices(store):
    store.add(ASIN, target_paise=TARGET, now=0)

    async def fetcher(asin):
        raise NotFoundError("Product page returned 404")

    result = await seed_product(store, ASIN, fetcher=fetcher, now=9)
    assert result.ok is False
    assert result.exit_code == 4
    assert result.error == "Product page returned 404"
    entry = store.require(ASIN)
    assert entry.current_paise == 0
    assert entry.lowest_paise == 0
    assert entry.last_error == "Product page returned 404"
    assert store.history(ASIN) == []


async def test_seed_product_requires_a_watched_asin(store):
    async def fetcher(asin):  # pragma: no cover
        raise AssertionError

    with pytest.raises(NotFoundError):
        await seed_product(store, "GHOST12345", fetcher=fetcher)


async def test_seed_product_needs_a_client_or_a_fetcher(store):
    store.add(ASIN, target_paise=TARGET, now=0)
    with pytest.raises(ValueError):
        await seed_product(store, ASIN)


# ===========================================================================
# input validation
# ===========================================================================


@pytest.mark.parametrize(
    "text, paise",
    [
        ("1999", 1_999_00),
        ("  1999  ", 1_999_00),
        ("2499.50", 2_499_50),
        ("2499.5", 2_499_50),
        ("1,72,490", 1_72_490_00),
        # Grouping commas are tolerated wherever they fall: they can never
        # change the numeric meaning, so a sloppy one is not worth an error.
        ("1,999,", 1_999_00),
        ("1,99,9", 1_999_00),
        ("Rs1999", 1_999_00),
        ("Rs.1999", 1_999_00),
        ("rs. 1999", 1_999_00),
        ("INR 1999", 1_999_00),
        ("₹ 1999", 1_999_00),
        ("1", 1_00),
        ("0.01", 1),
        ("10000000", 1_00_00_000_00),
    ],
)
def test_parse_target_paise_accepts(text, paise):
    got = parse_target_paise(text)
    assert got == paise
    assert isinstance(got, int)


@pytest.mark.parametrize(
    "text",
    [
        "0",
        "0.00",
        "-1",
        "-1999",
        "abc",
        "",
        "   ",
        None,
        "1999rupees",
        "1e9",
        "1999.999",
        "10000001",
        "99999999999",
        "1999 2999",
        "Rs",
        ".50",
        "NaN",
        "inf",
    ],
)
def test_parse_target_paise_rejects(text):
    with pytest.raises(InputError) as exc:
        parse_target_paise(text)
    assert exc.value.exit_code == 2


@pytest.mark.parametrize("raw, want", [("b0bzp2h373", ASIN), ("  B0BZP2H373 ", ASIN), (ASIN, ASIN)])
def test_normalize_asin_accepts(raw, want):
    assert normalize_asin(raw) == want


@pytest.mark.parametrize("raw", ["", "SHORT", "B0BZP2H37", "B0BZP2H3730", "B0BZP2H37!", None])
def test_normalize_asin_rejects(raw):
    with pytest.raises(InputError) as exc:
        normalize_asin(raw)
    assert exc.value.exit_code == 2


# ===========================================================================
# sparkline
# ===========================================================================


def test_sparkline_empty_is_empty():
    assert sparkline([]) == ""
    assert sparkline([], width=12) == ""


def test_sparkline_single_point():
    assert sparkline([1_999_00]) == "▄"
    assert sparkline([1_999_00], width=12) == "▄"


def test_sparkline_flat_series_does_not_divide_by_zero():
    assert sparkline([5_00] * 9) == "▄" * 9
    assert sparkline([0, 0, 0]) == "▄" * 3


def test_sparkline_spans_the_full_block_range():
    line = sparkline([100, 200, 300, 400, 500, 600, 700, 800])
    assert line[0] == "▁"
    assert line[-1] == "█"
    assert len(line) == 8


def test_sparkline_huge_range_is_fine():
    line = sparkline([1, 1_00_00_000_00])
    assert line == "▁█"


def test_sparkline_ignores_scale():
    """Only the shape matters; a x1000 rescale renders identically."""
    small = [100, 250, 175, 400]
    assert sparkline(small) == sparkline([v * 1000 for v in small])


def test_sparkline_samples_down_to_width():
    values = list(range(1, 101))
    line = sparkline(values, width=10)
    assert len(line) == 10
    assert line[0] == "▁"
    assert line[-1] == "█"


def test_sparkline_width_one_keeps_the_latest():
    assert sparkline([1, 2, 3, 999], width=1) == "▄"


def test_sparkline_shorter_than_width_is_not_padded():
    assert len(sparkline([1, 2, 3], width=48)) == 3


def test_sparkline_is_deterministic():
    values = [random.Random(1).randrange(1, 10_000) for _ in range(200)]
    assert sparkline(values, 24) == sparkline(values, 24)


def test_sparkline_accepts_floats_without_going_float():
    assert sparkline([1.0, 2.0, 3.0]) == sparkline([1, 2, 3])


# ===========================================================================
# time-weighted average
# ===========================================================================


def _pts(*pairs):
    return [PricePoint(asin=ASIN, paise=p, recorded_at=t) for p, t in pairs]


def test_time_weighted_average_of_nothing_is_zero():
    assert time_weighted_average([]) == 0


def test_time_weighted_average_of_one_point_is_that_price():
    assert time_weighted_average(_pts((1_999_00, 100)), now=1_000) == 1_999_00


def test_time_weighted_average_weights_by_duration():
    # 100 for 9 hours, 200 for 1 hour -> 110.
    points = _pts((100_00, 0), (200_00, 9 * 3600))
    assert time_weighted_average(points, now=10 * 3600) == 110_00


def test_time_weighted_average_ignores_a_now_in_the_past():
    points = _pts((100_00, 0), (200_00, 100))
    assert time_weighted_average(points, now=0) == 100_00


def test_time_weighted_average_of_simultaneous_points_is_a_plain_mean():
    points = _pts((100_00, 5), (200_00, 5), (300_00, 5))
    assert time_weighted_average(points, now=5) == 200_00


def test_time_weighted_average_is_an_exact_int():
    points = _pts((1_00, 0), (2_00, 3))
    got = time_weighted_average(points, now=3)
    assert got == 1_00
    assert isinstance(got, int)


# ===========================================================================
# sorting
# ===========================================================================


def _w(asin, **kw):
    return Watched(asin, **kw)


def test_sort_by_added_then_asin():
    items = [_w("B", added_at=2), _w("A", added_at=1), _w("C", added_at=1)]
    assert [e.asin for e in sort_entries(items, "added")] == ["A", "C", "B"]


def test_sort_by_target():
    items = [_w("A", target_paise=3_00), _w("B", target_paise=1_00)]
    assert [e.asin for e in sort_entries(items, "target")] == ["B", "A"]


def test_sort_by_price_puts_unknown_last():
    items = [_w("A", current_paise=0), _w("B", current_paise=5_00), _w("C", current_paise=1_00)]
    assert [e.asin for e in sort_entries(items, "price")] == ["C", "B", "A"]


def test_sort_by_drop_puts_the_biggest_fall_first():
    items = [
        _w("A", current_paise=900, highest_paise=1000),   # -10%
        _w("B", current_paise=500, highest_paise=1000),   # -50%
        _w("C", current_paise=0, highest_paise=1000),     # unknown
    ]
    assert [e.asin for e in sort_entries(items, "drop")] == ["B", "A", "C"]


def test_sort_by_name_falls_back_to_asin():
    items = [_w("ZZZ", title="apple"), _w("AAA", title=""), _w("MMM", title="Banana")]
    assert [e.asin for e in sort_entries(items, "name")] == ["AAA", "ZZZ", "MMM"]


def test_sort_does_not_mutate_its_input():
    items = [_w("B", added_at=2), _w("A", added_at=1)]
    sort_entries(items, "added")
    assert [e.asin for e in items] == ["B", "A"]


def test_sort_rejects_an_unknown_key():
    with pytest.raises(InputError) as exc:
        sort_entries([], "cheapest")
    assert exc.value.exit_code == 2
