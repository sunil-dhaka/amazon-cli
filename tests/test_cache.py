"""The on-disk response cache.

A cache is never load-bearing: every failure mode here -- a corrupt file, an
unwritable directory, a half-written temp file -- must degrade to a plain miss.
A cache that raises is strictly worse than no cache at all, because it turns a
successful scrape into a crash.

The clock is driven by rewriting the timestamp header of an entry rather than
by sleeping, so expiry is tested exactly and instantly.
"""

import gzip
import os
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from amazon_cli.cache import ResponseCache, default_cache_dir, parse_duration
from amazon_cli.cli import cli
from amazon_cli.client.base import AmazonClient

from conftest import load_product

BASE = "https://www.amazon.in"


def backdate(cache: ResponseCache, key: str, age_seconds: float) -> None:
    """Rewrite an entry's stored-at header so it reads as `age_seconds` old.

    Cheaper and far more exact than sleeping, and it doubles as a check that the
    on-disk format is the documented `timestamp\\nhtml` one.
    """
    import time

    path = cache._path_for(key)
    raw = gzip.decompress(path.read_bytes()).decode("utf-8")
    _, _, body = raw.partition("\n")
    payload = f"{time.time() - age_seconds}\n{body}".encode("utf-8")
    path.write_bytes(gzip.compress(payload, 6))


# ------------------------------------------------------------- parse_duration

@pytest.mark.parametrize(
    "text,seconds",
    [
        ("30s", 30),
        ("10m", 600),
        ("2h", 7200),
        ("1d", 86400),
        ("5", 300),        # a bare number is minutes
        ("0", 0),          # an explicit zero disables the cache
        ("10M", 600),      # unit is case-insensitive
        ("1D", 86400),
        ("  10 m  ", 600),  # spaces around and inside
        ("365d", 365 * 86400),  # the documented ceiling is itself valid
    ],
)
def test_parse_duration_accepts_documented_forms(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "-5", "10x", "1.5h", "m", "10 20", "1e3", "0x10", None],
)
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError) as excinfo:
        parse_duration(text)
    msg = str(excinfo.value)
    # An actionable message names what was rejected and shows the shape that works.
    assert repr(text) in msg
    for form in ("30s", "10m", "2h", "1d"):
        assert form in msg


@pytest.mark.parametrize("text", ["999999999999d", "100000000000m", "366d", "9000h"])
def test_parse_duration_rejects_absurd_durations(text):
    """A TTL of a million years is a typo, and silently means 'cache forever'."""
    with pytest.raises(ValueError) as excinfo:
        parse_duration(text)
    assert "365d" in str(excinfo.value)


def test_parse_duration_is_not_confused_by_a_type_error():
    """`None` must be a clean ValueError, not a TypeError from the regex."""
    with pytest.raises(ValueError):
        parse_duration(None)


# ---------------------------------------------------------- default_cache_dir

def test_default_cache_dir_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "amz"


def test_default_cache_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_cache_dir() == tmp_path / ".cache" / "amz"


def test_default_cache_dir_does_not_create_anything(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "nope"))
    default_cache_dir()
    assert not (tmp_path / "nope").exists()


# ------------------------------------------------------------ round-tripping

def test_set_then_get_round_trips(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("/dp/B0BZP2H373", "<html>hello</html>")
    assert cache.get("/dp/B0BZP2H373") == "<html>hello</html>"


def test_round_trip_survives_unicode_and_newlines(tmp_path):
    cache = ResponseCache(600, tmp_path)
    html = "<html>\n  price ₹1,72,490 -- naïve   emdash — \n</html>\n"
    cache.set("k", html)
    assert cache.get("k") == html


def test_a_miss_on_an_unknown_key_is_none(tmp_path):
    assert ResponseCache(600, tmp_path).get("never-stored") is None


def test_different_keys_never_share_an_entry(tmp_path):
    """A key collision here serves one product's page for another."""
    cache = ResponseCache(600, tmp_path)
    cache.set("/s?k=a", "<html>A</html>")
    cache.set("/s?k=b", "<html>B</html>")
    assert cache.get("/s?k=a") == "<html>A</html>"
    assert cache.get("/s?k=b") == "<html>B</html>"


def test_entries_are_sharded_and_gzipped(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>x</html>")
    files = list(tmp_path.rglob("*.html.gz"))
    assert len(files) == 1
    # Sharded one level deep, and readable with plain gzip tooling.
    assert files[0].parent.parent == tmp_path
    assert gzip.decompress(files[0].read_bytes()).endswith(b"<html>x</html>")


# --------------------------------------------------------------------- expiry

def test_an_entry_inside_its_ttl_is_served(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>fresh</html>")
    backdate(cache, "k", 599)
    assert cache.get("k") == "<html>fresh</html>"


def test_an_entry_past_its_ttl_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>stale</html>")
    backdate(cache, "k", 601)
    assert cache.get("k") is None


def test_a_shorter_ttl_expires_an_entry_written_under_a_longer_one(tmp_path):
    """TTL is a read-time policy, so lowering `--cache` takes effect at once."""
    ResponseCache(86400, tmp_path).set("k", "<html>old</html>")
    backdate(ResponseCache(86400, tmp_path), "k", 120)
    assert ResponseCache(60, tmp_path).get("k") is None
    assert ResponseCache(600, tmp_path).get("k") == "<html>old</html>"


def test_a_future_timestamp_is_not_treated_as_expired(tmp_path):
    """Clock skew must not silently disable the cache."""
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>x</html>")
    backdate(cache, "k", -3600)
    assert cache.get("k") == "<html>x</html>"


# ------------------------------------------------------- corrupt entries miss

def _write_raw(cache: ResponseCache, key: str, data: bytes) -> Path:
    path = cache._path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_an_empty_file_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    _write_raw(cache, "k", b"")
    assert cache.get("k") is None


def test_a_non_gzip_file_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    _write_raw(cache, "k", b"<html>this was never gzipped</html>")
    assert cache.get("k") is None


def test_a_truncated_gzip_file_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>" + ("x" * 50_000) + "</html>")
    path = cache._path_for("k")
    path.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 2])
    assert cache.get("k") is None


def test_a_gzip_file_with_a_corrupt_deflate_stream_is_a_miss(tmp_path):
    """The nastiest case: a valid gzip header over a damaged payload.

    zlib raises `zlib.error`, which is *not* an OSError -- so a naive except
    clause lets it escape and crash the command it was meant to speed up.
    """
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>" + ("abcdefgh" * 4000) + "</html>")
    path = cache._path_for("k")
    raw = bytearray(path.read_bytes())
    for i in range(12, min(len(raw), 60)):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw))
    assert cache.get("k") is None


def test_a_gzip_file_of_invalid_utf8_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    _write_raw(cache, "k", gzip.compress(b"1700000000.0\n\xff\xfe not utf-8"))
    assert cache.get("k") is None


def test_an_entry_with_no_timestamp_header_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    _write_raw(cache, "k", gzip.compress(b"<html>no header line</html>"))
    assert cache.get("k") is None


def test_an_entry_with_a_non_numeric_timestamp_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    _write_raw(cache, "k", gzip.compress(b"yesterday\n<html>x</html>"))
    assert cache.get("k") is None


def test_a_directory_where_an_entry_should_be_is_a_miss(tmp_path):
    cache = ResponseCache(600, tmp_path)
    path = cache._path_for("k")
    path.mkdir(parents=True)
    assert cache.get("k") is None


def test_a_stray_temp_file_is_never_served(tmp_path):
    """The writer is write-then-rename; a leftover `.tmp` is torn by definition."""
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>real</html>")
    path = cache._path_for("k")
    stray = path.with_suffix(".tmp")
    stray.write_bytes(gzip.compress(b"9999999999.0\n<html>torn</html>"))

    assert cache.get("k") == "<html>real</html>"
    count, _ = cache.stats()
    assert count == 1, "a half-written .tmp file must not count as an entry"


# ------------------------------------------------------------ disabled cache

@pytest.mark.parametrize("ttl", [0, -1, -86400])
def test_a_disabled_cache_never_touches_the_disk(tmp_path, ttl):
    cache = ResponseCache(ttl, tmp_path)
    assert cache.enabled is False
    cache.set("k", "<html>x</html>")
    assert cache.get("k") is None
    assert list(tmp_path.rglob("*")) == []


def test_an_empty_body_is_never_stored(tmp_path):
    """Caching an empty page would pin a failed fetch for the whole TTL."""
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "")
    assert cache.get("k") is None
    assert list(tmp_path.rglob("*")) == []


# ------------------------------------------------------------ hostile filesystem

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_directory_degrades_to_a_miss(tmp_path):
    directory = tmp_path / "readonly"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        cache = ResponseCache(600, directory)
        cache.set("k", "<html>x</html>")  # must not raise
        assert cache.get("k") is None
        assert cache.stats() == (0, 0)
        assert cache.clear() == 0
    finally:
        directory.chmod(0o700)


def test_a_cache_directory_that_is_actually_a_file_degrades_to_a_miss(tmp_path):
    not_a_dir = tmp_path / "amz"
    not_a_dir.write_text("I am a file")
    cache = ResponseCache(600, not_a_dir)
    cache.set("k", "<html>x</html>")  # must not raise
    assert cache.get("k") is None
    assert cache.stats() == (0, 0)
    assert cache.clear() == 0


def test_a_missing_directory_reports_empty_stats_rather_than_raising(tmp_path):
    cache = ResponseCache(600, tmp_path / "does-not-exist")
    assert cache.stats() == (0, 0)
    assert cache.clear() == 0


# ---------------------------------------------------------------- clear/stats

def test_stats_counts_entries_and_bytes(tmp_path):
    cache = ResponseCache(600, tmp_path)
    for i in range(5):
        cache.set(f"key-{i}", f"<html>{'y' * 1000}{i}</html>")

    count, size = cache.stats()
    assert count == 5
    on_disk = sum(p.stat().st_size for p in tmp_path.rglob("*.html.gz"))
    assert size == on_disk > 0


def test_stats_is_zero_for_a_fresh_directory(tmp_path):
    assert ResponseCache(600, tmp_path).stats() == (0, 0)


def test_clear_returns_the_number_of_entries_removed(tmp_path):
    cache = ResponseCache(600, tmp_path)
    for i in range(7):
        cache.set(f"key-{i}", f"<html>{i}</html>")
    assert cache.stats()[0] == 7

    assert cache.clear() == 7
    assert cache.stats() == (0, 0)
    assert list(tmp_path.rglob("*.html.gz")) == []


def test_clear_on_an_empty_cache_returns_zero(tmp_path):
    assert ResponseCache(600, tmp_path).clear() == 0


def test_clear_also_reclaims_leftover_temp_files(tmp_path):
    """A crashed write leaks a multi-MB `.tmp` that nothing else ever reclaims."""
    cache = ResponseCache(600, tmp_path)
    for i in range(3):
        cache.set(f"key-{i}", f"<html>{i}</html>")
    stray = cache._path_for("key-0").with_suffix(".tmp")
    stray.write_bytes(gzip.compress(b"1700000000.0\n<html>torn</html>"))

    assert cache.clear() == 3, "the count reports real entries, not debris"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_clear_does_not_touch_unrelated_files(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>x</html>")
    keep = tmp_path / "README.txt"
    keep.write_text("not ours")
    assert cache.clear() == 1
    assert keep.exists()


# -------------------------------------------------- the cache saves requests

@respx.mock
async def test_a_second_fetch_of_the_same_url_makes_no_request(tmp_path):
    page = load_product("B0BZP2H373")
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=page)
    )
    cache = ResponseCache(600, tmp_path)
    async with AmazonClient(cache=cache) as client:
        first = await client.fetch("/dp/B0BZP2H373")
        second = await client.fetch("/dp/B0BZP2H373")

    assert first == second == page
    assert route.call_count == 1, "the second fetch must be served from disk"


@respx.mock
async def test_an_expired_entry_forces_a_real_request(tmp_path):
    page = load_product("B0BZP2H373")
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=page)
    )
    cache = ResponseCache(60, tmp_path)
    async with AmazonClient(cache=cache) as client:
        await client.fetch("/dp/B0BZP2H373")
        backdate(cache, "/dp/B0BZP2H373", 61)
        again = await client.fetch("/dp/B0BZP2H373")

    assert again == page
    assert route.call_count == 2


@respx.mock
async def test_a_disabled_cache_never_saves_a_request(tmp_path):
    page = load_product("B0BZP2H373")
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=page)
    )
    async with AmazonClient(cache=ResponseCache(0, tmp_path)) as client:
        await client.fetch("/dp/B0BZP2H373")
        await client.fetch("/dp/B0BZP2H373")

    assert route.call_count == 2
    assert list(tmp_path.rglob("*")) == []


@respx.mock
async def test_two_searches_with_different_terms_do_not_collide(tmp_path):
    """A shared key here would serve one search's page for another."""
    a = respx.get(f"{BASE}/s", params={"k": "aaa"}).mock(
        return_value=httpx.Response(200, text="<html>AAA results</html>")
    )
    b = respx.get(f"{BASE}/s", params={"k": "bbb"}).mock(
        return_value=httpx.Response(200, text="<html>BBB results</html>")
    )
    cache = ResponseCache(600, tmp_path)
    async with AmazonClient(cache=cache) as client:
        assert await client.fetch("/s", params={"k": "aaa"}) == "<html>AAA results</html>"
        assert await client.fetch("/s", params={"k": "bbb"}) == "<html>BBB results</html>"
        # Both are now cached and must still come back distinct.
        assert await client.fetch("/s", params={"k": "aaa"}) == "<html>AAA results</html>"
        assert await client.fetch("/s", params={"k": "bbb"}) == "<html>BBB results</html>"

    assert a.call_count == 1
    assert b.call_count == 1
    assert cache.stats()[0] == 2


@respx.mock
async def test_the_same_query_on_different_pages_does_not_collide(tmp_path):
    # One route with a handler, because respx `params=` matching is subset-based:
    # two routes would let `k=x` swallow `k=x&page=2` and hide the real answer.
    seen = []

    def handler(request):
        page = request.url.params.get("page", "1")
        seen.append(page)
        return httpx.Response(200, text=f"<html>page {page}</html>")

    respx.get(f"{BASE}/s").mock(side_effect=handler)
    cache = ResponseCache(600, tmp_path)
    async with AmazonClient(cache=cache) as client:
        assert await client.fetch("/s", params={"k": "x"}) == "<html>page 1</html>"
        assert await client.fetch("/s", params={"k": "x", "page": 2}) == "<html>page 2</html>"
        assert await client.fetch("/s", params={"k": "x"}) == "<html>page 1</html>"
        assert await client.fetch("/s", params={"k": "x", "page": 2}) == "<html>page 2</html>"

    assert seen == ["1", "2"], "each distinct URL is fetched exactly once"
    assert cache.stats()[0] == 2


@respx.mock
async def test_a_param_order_change_still_hits_the_same_entry(tmp_path):
    """Keys are built from sorted params, so option order cannot split the cache."""
    route = respx.get(f"{BASE}/s").mock(
        return_value=httpx.Response(200, text="<html>results</html>")
    )
    async with AmazonClient(cache=ResponseCache(600, tmp_path)) as client:
        await client.fetch("/s", params={"k": "x", "s": "review-rank"})
        await client.fetch("/s", params={"s": "review-rank", "k": "x"})
    assert route.call_count == 1


@respx.mock
async def test_a_failed_fetch_is_never_cached(tmp_path):
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    cache = ResponseCache(600, tmp_path)
    async with AmazonClient(max_retries=0, cache=cache) as client:
        with pytest.raises(Exception):
            await client.fetch("/dp/B0ZZZZZZZZ")
    assert cache.stats() == (0, 0)


@respx.mock
async def test_a_bot_check_is_never_cached(tmp_path, botcheck_page):
    respx.get(f"{BASE}/s").mock(return_value=httpx.Response(200, text=botcheck_page))
    cache = ResponseCache(600, tmp_path)
    async with AmazonClient(max_retries=0, cache=cache) as client:
        with pytest.raises(Exception):
            await client.fetch("/s", params={"k": "headphones"})
    assert cache.stats() == (0, 0), "caching a bot check would poison the cache"


# --------------------------------------------------------- `amz cache` command

def test_cache_path_prints_the_configured_directory(tmp_path):
    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(tmp_path)


def test_cache_stats_reports_a_populated_cache(tmp_path):
    cache = ResponseCache(600, tmp_path)
    for i in range(3):
        cache.set(f"key-{i}", "<html>" + "z" * 5000 + "</html>")

    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "stats"])
    assert result.exit_code == 0
    assert "Entries:   3" in result.output
    assert str(tmp_path) in result.output


def test_cache_stats_on_an_empty_directory(tmp_path):
    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "stats"])
    assert result.exit_code == 0
    assert "Entries:   0" in result.output
    assert "0 B" in result.output


def test_cache_clear_removes_entries_with_yes(tmp_path):
    cache = ResponseCache(600, tmp_path)
    for i in range(4):
        cache.set(f"key-{i}", f"<html>{i}</html>")

    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "clear", "-y"])
    assert result.exit_code == 0
    assert "Removed 4 cached entries." in result.output
    assert cache.stats() == (0, 0)


def test_cache_clear_singular_wording_for_one_entry(tmp_path):
    ResponseCache(600, tmp_path).set("k", "<html>x</html>")
    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "clear", "-y"])
    assert result.exit_code == 0
    assert "Removed 1 cached entry." in result.output


def test_cache_clear_prompts_and_aborts_on_no(tmp_path):
    cache = ResponseCache(600, tmp_path)
    cache.set("k", "<html>x</html>")

    result = CliRunner().invoke(
        cli, ["--cache-dir", str(tmp_path), "cache", "clear"], input="n\n"
    )
    assert result.exit_code != 0
    assert cache.stats()[0] == 1, "aborting must not delete anything"


def test_cache_clear_on_an_empty_cache_says_so(tmp_path):
    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path), "cache", "clear"])
    assert result.exit_code == 0
    assert "already empty" in result.output


def test_cache_commands_never_touch_the_real_cache_directory(tmp_path, monkeypatch):
    """`--cache-dir` has to win outright, or a test run nukes a user's cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    result = CliRunner().invoke(cli, ["--cache-dir", str(tmp_path / "explicit"), "cache", "path"])
    assert result.output.strip() == str(tmp_path / "explicit")
    assert not (tmp_path / "xdg").exists()
