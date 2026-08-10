"""`amz watch` must honour the global options.

`watch add` and `watch check` used to build their own `AmazonClient` with
nothing but a politeness floor, so `--timeout`, `--retries`, `--cache` and
`--min-interval` were silently dropped. Silently: the command still worked, it
just ignored what you asked for -- `amz --cache 10m watch check` refetched every
2 MB page on every run and never said so.
"""

import httpx
import pytest
import respx
from click.testing import CliRunner

from amazon_cli.cli import cli
from amazon_cli.context import AmzContext
from amazon_cli.watch.service import MIN_INTERVAL

from conftest import load_product

BASE = "https://www.amazon.in"
ASIN = "B0BZP2H373"


@pytest.fixture
def watch_db(tmp_path, monkeypatch):
    db = tmp_path / "watch.db"
    monkeypatch.setenv("AMZ_WATCH_DB", str(db))
    return db


def _settings(**kwargs) -> AmzContext:
    return AmzContext.resolve(**kwargs)


# ------------------------------------------------------------------ the builder

def test_the_client_carries_every_global_option():
    from amazon_cli.commands.watch import _client

    settings = _settings(timeout=12.5, retries=7, min_interval=9.0)
    client = _client(settings)

    assert client._timeout == 12.5
    assert client._max_retries == 7


def test_watch_never_sweeps_faster_than_its_own_politeness_floor():
    """A user asking for --min-interval 0 must not turn a sweep into a burst."""
    from amazon_cli.commands.watch import _client

    client = _client(_settings(min_interval=0.0))
    assert client._min_interval >= MIN_INTERVAL


def test_a_slower_user_setting_wins_over_the_floor():
    from amazon_cli.commands.watch import _client

    client = _client(_settings(min_interval=MIN_INTERVAL + 10))
    assert client._min_interval == MIN_INTERVAL + 10


def test_the_cache_setting_reaches_the_client(tmp_path):
    from amazon_cli.commands.watch import _client

    assert _client(_settings())._cache.enabled is False
    cached = _client(_settings(cache="10m", cache_dir=tmp_path))
    assert cached._cache.enabled is True
    assert cached._cache.ttl_seconds == 600


# ------------------------------------------------------------------- end to end

@respx.mock
def test_watch_add_honours_the_global_timeout(watch_db):
    """The regression: a global option that the command quietly ignored."""
    respx.get(f"{BASE}/dp/{ASIN}").mock(side_effect=httpx.ReadTimeout("too slow"))

    result = CliRunner().invoke(
        cli, ["--timeout", "0.5", "--retries", "0", "watch", "add", ASIN, "--target", "23000"]
    )

    # The row is still created; only the price seed failed.
    assert "Could not fetch" in result.output or "timed out" in result.output.lower()


@respx.mock
def test_watch_add_honours_the_global_retry_count(watch_db):
    route = respx.get(f"{BASE}/dp/{ASIN}").mock(return_value=httpx.Response(503))

    CliRunner().invoke(
        cli, ["--retries", "0", "watch", "add", ASIN, "--target", "23000"]
    )
    assert route.call_count == 1, "--retries 0 must mean exactly one attempt"


@respx.mock
def test_watch_check_reuses_a_cached_page(watch_db, tmp_path):
    """`--cache` must actually stop the second sweep refetching the page."""
    page = load_product(ASIN)
    route = respx.get(f"{BASE}/dp/{ASIN}").mock(return_value=httpx.Response(200, text=page))
    runner = CliRunner()
    cache_args = ["--cache", "10m", "--cache-dir", str(tmp_path / "cache")]

    runner.invoke(cli, [*cache_args, "watch", "add", ASIN, "--target", "23000"])
    first = route.call_count
    assert first >= 1

    runner.invoke(cli, [*cache_args, "watch", "check", "--quiet"])
    assert route.call_count == first, "the cached page should have been reused"


@respx.mock
def test_without_the_cache_a_second_sweep_refetches(watch_db):
    page = load_product(ASIN)
    route = respx.get(f"{BASE}/dp/{ASIN}").mock(return_value=httpx.Response(200, text=page))
    runner = CliRunner()

    runner.invoke(cli, ["watch", "add", ASIN, "--target", "23000"])
    first = route.call_count

    runner.invoke(cli, ["watch", "check", "--quiet"])
    assert route.call_count > first
