"""Batch and concurrent fetching for `amz product` and `amz compare`.

Two things have to hold at once here. A batch must be *resilient* -- one dead
ASIN cannot sink the nine good ones the user already paid for. And it must stay
*polite* -- concurrency is a cap, not a suggestion, because bursting is exactly
what gets this client served a bot check.

The third thing, easiest to break and hardest to notice: adding batch support
must not have changed what a single ASIN prints. `amz product X --json` has to
stay a bare object, or every `| jq .price` in the wild breaks.
"""

import asyncio
import json
import time

import httpx
import pytest
import respx
from click.testing import CliRunner

from amazon_cli.cli import cli
from amazon_cli.commands import product as product_cmd
from amazon_cli.context import AmzContext
from amazon_cli.errors import NetworkError, NotFoundError, RateLimitedError

from conftest import load_product

BASE = "https://www.amazon.in"

GOOD = ["B0BZP2H373", "B0C3ZYFZ77", "1847941834"]


def mock_product(asin: str, html: str | None = None):
    """Serve a captured product page for `asin`."""
    page = load_product(asin) if html is None else html
    return respx.get(f"{BASE}/dp/{asin}").mock(
        return_value=httpx.Response(200, text=page)
    )


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


# ---------------------------------------------------------------- happy batch

@respx.mock
def test_three_asins_produce_three_results():
    for asin in GOOD:
        mock_product(asin)
    result = run("product", *GOOD, "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [p["asin"] for p in payload] == GOOD


@respx.mock
def test_batch_output_preserves_argument_order():
    """Concurrency must not reorder results -- ASIN 1 stays first."""
    ordered = list(reversed(GOOD))
    for asin in ordered:
        mock_product(asin)
    result = run("product", *ordered, "--json")
    assert [p["asin"] for p in json.loads(result.stdout)] == ordered


@respx.mock
def test_a_repeated_asin_is_fetched_once_per_argument_but_reported_twice():
    route = mock_product("B0BZP2H373")
    result = run("product", "B0BZP2H373", "B0BZP2H373", "--json")
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 2
    assert route.call_count == 2, "no cache configured, so both are real fetches"


# ------------------------------------------------------- single-ASIN contract

@respx.mock
def test_a_single_asin_json_is_a_bare_object_not_a_list():
    """The one output shape that batch support was most likely to break."""
    mock_product("B0BZP2H373")
    result = run("product", "B0BZP2H373", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), "a single ASIN must not become a one-element array"
    assert payload["asin"] == "B0BZP2H373"
    assert not result.output.lstrip().startswith("[")


@respx.mock
def test_two_asins_json_is_a_list_even_when_one_fails():
    mock_product("B0BZP2H373")
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("product", "B0BZP2H373", "B0ZZZZZZZZ", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 1


@respx.mock
def test_a_single_asin_plain_output_has_one_header_and_one_row():
    mock_product("B0BZP2H373")
    result = run("product", "B0BZP2H373", "--plain")
    assert result.exit_code == 0
    lines = result.output.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].split("\t") == product_cmd.PLAIN_HEADERS
    assert lines[1].split("\t")[0] == "B0BZP2H373"


@respx.mock
def test_plain_price_stays_in_paise_for_existing_consumers():
    """`--plain`'s `price` column is paise and must not quietly become rupees."""
    mock_product("B0BZP2H373")
    plain = run("product", "B0BZP2H373", "--plain").output.strip().split("\n")
    as_json = json.loads(run("product", "B0BZP2H373", "--json").stdout)
    cells = dict(zip(product_cmd.PLAIN_HEADERS, plain[1].split("\t")))
    assert int(cells["price"]) == as_json["price_paise"]


# ---------------------------------------------------------- partial failures

@respx.mock
def test_one_bad_asin_does_not_sink_the_batch():
    mock_product("B0BZP2H373")
    mock_product("B0C3ZYFZ77")
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))

    result = run("product", "B0BZP2H373", "B0ZZZZZZZZ", "B0C3ZYFZ77", "--json")
    assert result.exit_code == 0
    assert [p["asin"] for p in json.loads(result.stdout)] == ["B0BZP2H373", "B0C3ZYFZ77"]


@respx.mock
def test_a_partial_failure_warns_on_stderr_and_keeps_stdout_parseable():
    """The warning must not land in stdout, or `| jq` chokes on it."""
    mock_product("B0BZP2H373")
    mock_product("B0C3ZYFZ77")
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))

    result = run("product", "B0BZP2H373", "B0ZZZZZZZZ", "B0C3ZYFZ77", "--json")
    assert result.exit_code == 0
    assert "B0ZZZZZZZZ" in result.stderr
    assert "Warning" in result.stderr
    assert "Warning" not in result.stdout
    # stdout is JSON and nothing else.
    payload = json.loads(result.stdout)
    assert [p["asin"] for p in payload] == ["B0BZP2H373", "B0C3ZYFZ77"]


@respx.mock
def test_a_malformed_asin_in_a_batch_is_reported_not_fatal():
    mock_product("B0BZP2H373")
    result = run("product", "B0BZP2H373", "not-an-asin", "--json")
    assert result.exit_code == 0
    assert "not-an-asin" in result.stderr.lower() or "NOT-AN-ASIN" in result.stderr
    assert json.loads(result.stdout)[0]["asin"] == "B0BZP2H373"


# ----------------------------------------------------------- total failure

@respx.mock
def test_a_single_failing_asin_exits_with_that_errors_code():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("product", "B0ZZZZZZZZ")
    assert result.exit_code == NotFoundError.exit_code == 4
    assert "Not found" in result.output


@respx.mock
def test_a_whole_batch_failing_with_one_cause_keeps_that_exit_code():
    for asin in ("B0ZZZZZZZZ", "B0YYYYYYYY"):
        respx.get(f"{BASE}/dp/{asin}").mock(return_value=httpx.Response(404))
    result = run("product", "B0ZZZZZZZZ", "B0YYYYYYYY")
    assert result.exit_code == 4
    assert "All 2 product lookups failed." in result.output


@respx.mock
def test_a_batch_failing_for_mixed_reasons_exits_generic():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/dp/B0YYYYYYYY").mock(return_value=httpx.Response(500))
    result = run("--retries", "0", "product", "B0ZZZZZZZZ", "B0YYYYYYYY")
    assert result.exit_code == 1
    assert "All 2 product lookups failed." in result.output


@respx.mock
def test_a_throttled_batch_exits_five():
    for asin in ("B0BZP2H373", "B0C3ZYFZ77"):
        respx.get(f"{BASE}/dp/{asin}").mock(return_value=httpx.Response(429))
    result = run("--retries", "0", "product", "B0BZP2H373", "B0C3ZYFZ77")
    assert result.exit_code == RateLimitedError.exit_code == 5


def test_no_asins_at_all_is_a_usage_error():
    result = run("product")
    assert result.exit_code == 2


# ------------------------------------------------------------ concurrency cap

class Tracker:
    """Records the high-water mark of simultaneously in-flight fetches."""

    def __init__(self, hold: float = 0.02):
        self.hold = hold
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self, client, asin):
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.hold)
            return type("Stub", (), {"asin": asin, "to_dict": lambda self: {"asin": asin}})()
        finally:
            self.inflight -= 1


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_concurrency_is_a_hard_cap_not_a_hint(monkeypatch, limit):
    tracker = Tracker()
    monkeypatch.setattr(product_cmd, "get_product", tracker)

    asins = [f"B{i:09d}" for i in range(9)]
    details, failures = asyncio.run(
        product_cmd._fetch_all(AmzContext.resolve(), asins, limit)
    )

    assert failures == []
    assert len(details) == 9
    assert tracker.calls == 9
    assert tracker.peak <= limit, f"{tracker.peak} fetches were in flight with --concurrency {limit}"


def test_a_high_concurrency_actually_overlaps(monkeypatch):
    """The cap must not be so eager that it serialises everything."""
    tracker = Tracker()
    monkeypatch.setattr(product_cmd, "get_product", tracker)

    asins = [f"B{i:09d}" for i in range(8)]
    asyncio.run(product_cmd._fetch_all(AmzContext.resolve(), asins, 4))
    assert tracker.peak == 4


@pytest.mark.parametrize("bad", [0, -1])
def test_concurrency_zero_or_negative_is_a_usage_error(bad):
    result = run("product", "B0BZP2H373", "--concurrency", str(bad))
    assert result.exit_code == 2


def test_fetch_all_defends_itself_against_a_zero_concurrency(monkeypatch):
    """`_fetch_all` is called from two commands; it must not deadlock on 0."""
    tracker = Tracker()
    monkeypatch.setattr(product_cmd, "get_product", tracker)
    details, failures = asyncio.run(
        product_cmd._fetch_all(AmzContext.resolve(), ["B000000001"], 0)
    )
    assert len(details) == 1 and failures == []


@respx.mock
def test_concurrency_cap_holds_over_real_http():
    inflight = peak = 0

    async def handler(request):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(200, text=load_product("B0BZP2H373"))
        finally:
            inflight -= 1

    for asin in GOOD:
        respx.get(f"{BASE}/dp/{asin}").mock(side_effect=handler)

    result = run("product", *GOOD, "--concurrency", "1", "--json")
    assert result.exit_code == 0
    assert peak == 1


# -------------------------------------------------------------- min-interval

@respx.mock
def test_min_interval_paces_a_batch():
    for asin in GOOD:
        mock_product(asin)

    started = time.monotonic()
    result = run("--min-interval", "0.15", "product", *GOOD, "--json")
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 3
    # Three requests at a 0.15s floor cannot finish in under two gaps.
    assert elapsed >= 0.3, f"batch finished in {elapsed:.3f}s -- --min-interval was ignored"


@respx.mock
def test_without_min_interval_a_batch_is_not_paced():
    for asin in GOOD:
        mock_product(asin)
    started = time.monotonic()
    assert run("product", *GOOD, "--json").exit_code == 0
    assert time.monotonic() - started < 0.3


# ------------------------------------------------------------------- compare

@respx.mock
def test_compare_needs_at_least_two_asins():
    result = run("compare", "B0BZP2H373")
    assert result.exit_code == 2
    assert "at least 2" in result.output


@respx.mock
def test_compare_fetches_every_asin():
    for asin in GOOD:
        mock_product(asin)
    result = run("compare", *GOOD, "--json")
    assert result.exit_code == 0
    assert [p["asin"] for p in json.loads(result.stdout)] == GOOD


@respx.mock
def test_compare_survives_one_bad_asin():
    mock_product("B0BZP2H373")
    mock_product("B0C3ZYFZ77")
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("compare", "B0BZP2H373", "B0ZZZZZZZZ", "B0C3ZYFZ77", "--json")
    assert result.exit_code == 0
    assert "B0ZZZZZZZZ" in result.stderr
    assert [p["asin"] for p in json.loads(result.stdout)] == ["B0BZP2H373", "B0C3ZYFZ77"]


@respx.mock
def test_compare_with_every_asin_failing_is_non_zero():
    for asin in ("B0ZZZZZZZZ", "B0YYYYYYYY"):
        respx.get(f"{BASE}/dp/{asin}").mock(return_value=httpx.Response(404))
    result = run("compare", "B0ZZZZZZZZ", "B0YYYYYYYY")
    assert result.exit_code != 0
    assert "All product lookups failed." in result.output


@respx.mock
def test_compare_honours_its_own_concurrency_cap():
    """`compare` carries a second copy of the semaphore logic; it can drift."""
    inflight = peak = 0

    async def handler(request):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(200, text=load_product("B0BZP2H373"))
        finally:
            inflight -= 1

    for asin in GOOD:
        respx.get(f"{BASE}/dp/{asin}").mock(side_effect=handler)

    result = run("compare", *GOOD, "--concurrency", "1", "--json")
    assert result.exit_code == 0
    assert peak == 1


@respx.mock
def test_compare_json_is_always_a_list_even_for_two():
    for asin in GOOD[:2]:
        mock_product(asin)
    payload = json.loads(run("compare", *GOOD[:2], "--json").stdout)
    assert isinstance(payload, list) and len(payload) == 2


# ------------------------------------------------------ batches and the cache

@respx.mock
def test_a_batch_shares_its_cache_across_asins(tmp_path):
    """`amz compare A A B` must not fetch A twice when the cache is on."""
    a = mock_product("B0BZP2H373")
    b = mock_product("B0C3ZYFZ77")
    result = run(
        "--cache", "10m", "--cache-dir", str(tmp_path),
        "product", "B0BZP2H373", "B0C3ZYFZ77", "B0BZP2H373", "--json",
    )
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 3
    # Concurrent siblings can race past a cold cache, so the guarantee is
    # "no more than one request per distinct URL was *needed*", checked on a
    # second, fully warm invocation.
    before = (a.call_count, b.call_count)
    run(
        "--cache", "10m", "--cache-dir", str(tmp_path),
        "product", "B0BZP2H373", "B0C3ZYFZ77", "--json",
    )
    assert (a.call_count, b.call_count) == before, "a warm cache must serve the whole batch"


@respx.mock
def test_the_batch_cache_does_not_serve_one_asin_for_another(tmp_path):
    mock_product("B0BZP2H373")
    mock_product("B0C3ZYFZ77")
    args = ["--cache", "10m", "--cache-dir", str(tmp_path),
            "product", "B0BZP2H373", "B0C3ZYFZ77", "--json"]
    run(*args)
    warm = json.loads(run(*args).stdout)
    assert [p["asin"] for p in warm] == ["B0BZP2H373", "B0C3ZYFZ77"]
    assert warm[0]["title"] != warm[1]["title"]


# ------------------------------------------------------------- network errors

@respx.mock
def test_retries_zero_means_exactly_one_request():
    """Proves `--retries` actually reaches the client the batch builds."""
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(return_value=httpx.Response(503))
    result = run("--retries", "0", "product", "B0BZP2H373")
    assert result.exit_code == 5
    assert route.call_count == 1


@respx.mock
def test_a_connection_failure_in_a_batch_is_a_network_error():
    respx.get(f"{BASE}/dp/B0BZP2H373").mock(side_effect=httpx.ConnectError("down"))
    result = run("--retries", "0", "product", "B0BZP2H373")
    assert result.exit_code == NetworkError.exit_code == 3
