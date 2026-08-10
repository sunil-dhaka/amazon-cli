"""Global option resolution.

`AmzContext` is the single place raw `--cache`/`--timeout`/`--retries`/
`--min-interval` values become validated settings. If it silently accepts
nonsense, every command inherits the nonsense; if it drops a value, the flag
becomes decorative. Both failures are invisible without these tests.
"""

from pathlib import Path

import click
import pytest

from amazon_cli.cache import ResponseCache, default_cache_dir
from amazon_cli.client.base import AmazonClient
from amazon_cli.context import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    AmzContext,
    to_amz_error,
)
from amazon_cli.errors import (
    AmzError,
    BotCheckError,
    InputError,
    NetworkError,
    NotFoundError,
    ParseError,
    RateLimitedError,
)


# ------------------------------------------------------------------- defaults

def test_bare_defaults_disable_the_cache():
    settings = AmzContext.resolve()
    assert settings.cache_ttl == 0
    assert settings.cache().enabled is False
    assert settings.timeout == DEFAULT_TIMEOUT
    assert settings.retries == DEFAULT_RETRIES
    assert settings.min_interval == DEFAULT_MIN_INTERVAL
    assert settings.debug is False


def test_default_concurrency_is_polite_but_useful():
    assert 1 < DEFAULT_CONCURRENCY <= 8


# ---------------------------------------------------------------- cache flags

@pytest.mark.parametrize(
    "text,seconds", [("30s", 30), ("10m", 600), ("2h", 7200), ("1d", 86400), ("15", 900)]
)
def test_cache_duration_is_parsed(text, seconds):
    assert AmzContext.resolve(cache=text).cache_ttl == seconds


def test_no_cache_beats_cache():
    """A shell alias carrying `--cache 10m` must be overridable in place."""
    settings = AmzContext.resolve(cache="10m", no_cache=True)
    assert settings.cache_ttl == 0
    assert settings.cache().enabled is False


def test_no_cache_alone_is_still_disabled():
    assert AmzContext.resolve(no_cache=True).cache_ttl == 0


def test_cache_zero_disables_without_erroring():
    assert AmzContext.resolve(cache="0").cache_ttl == 0


@pytest.mark.parametrize("bad", ["bogus", "", "-5", "1.5h", "10x", "999999999999d"])
def test_a_bad_cache_duration_is_an_input_error(bad):
    with pytest.raises(InputError) as excinfo:
        AmzContext.resolve(cache=bad)
    assert excinfo.value.exit_code == 2
    assert str(excinfo.value)  # never an empty message


def test_cache_dir_is_coerced_to_a_path(tmp_path):
    settings = AmzContext.resolve(cache="10m", cache_dir=str(tmp_path))
    assert settings.cache_directory == tmp_path
    assert isinstance(settings.cache_directory, Path)


def test_cache_directory_falls_back_to_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert AmzContext.resolve().cache_directory == default_cache_dir()


def test_cache_builds_a_response_cache_with_both_settings(tmp_path):
    cache = AmzContext.resolve(cache="10m", cache_dir=tmp_path).cache()
    assert isinstance(cache, ResponseCache)
    assert cache.ttl_seconds == 600
    assert cache.directory == tmp_path
    assert cache.enabled is True


# ----------------------------------------------------------- numeric validation

@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_a_non_positive_timeout_is_rejected(bad):
    with pytest.raises(InputError) as excinfo:
        AmzContext.resolve(timeout=bad)
    assert excinfo.value.exit_code == 2
    assert "--timeout" in str(excinfo.value)


@pytest.mark.parametrize("bad", [-1, -100])
def test_negative_retries_are_rejected(bad):
    with pytest.raises(InputError) as excinfo:
        AmzContext.resolve(retries=bad)
    assert "--retries" in str(excinfo.value)


@pytest.mark.parametrize("bad", [-0.1, -5])
def test_a_negative_min_interval_is_rejected(bad):
    with pytest.raises(InputError) as excinfo:
        AmzContext.resolve(min_interval=bad)
    assert "--min-interval" in str(excinfo.value)


def test_zero_retries_and_zero_interval_are_legal():
    settings = AmzContext.resolve(retries=0, min_interval=0)
    assert settings.retries == 0
    assert settings.min_interval == 0.0


def test_numeric_settings_are_coerced_to_their_declared_types():
    """Click hands over ints for `--timeout 5`; the dataclass promises floats."""
    settings = AmzContext.resolve(timeout=5, retries=7, min_interval=2)
    assert isinstance(settings.timeout, float) and settings.timeout == 5.0
    assert isinstance(settings.retries, int) and settings.retries == 7
    assert isinstance(settings.min_interval, float) and settings.min_interval == 2.0


# ---------------------------------------------------------------- client wiring

def test_client_carries_every_global_option(tmp_path):
    settings = AmzContext.resolve(
        cache="10m", cache_dir=tmp_path, timeout=7.5, retries=1, min_interval=0.25
    )
    client = settings.client()
    assert isinstance(client, AmazonClient)
    assert client._timeout == 7.5
    assert client._max_retries == 1
    assert client._min_interval == 0.25
    assert client._cache.ttl_seconds == 600
    assert client._cache.directory == tmp_path


def test_client_without_a_cache_flag_gets_a_disabled_cache():
    assert AmzContext.resolve().client()._cache.enabled is False


# ------------------------------------------------------------------- current()

def test_current_without_any_click_context_returns_defaults():
    settings = AmzContext.current(None)
    assert isinstance(settings, AmzContext)
    assert settings.cache_ttl == 0
    assert settings.timeout == DEFAULT_TIMEOUT


def test_current_returns_the_object_on_the_context():
    ctx = click.Context(click.Command("x"))
    ctx.obj = AmzContext.resolve(cache="2h", timeout=9)
    got = AmzContext.current(ctx)
    assert got.cache_ttl == 7200
    assert got.timeout == 9.0


@pytest.mark.parametrize("junk", [None, "not-a-context-object", 42, {}])
def test_current_ignores_a_foreign_context_object(junk):
    """Another library's `ctx.obj` must not crash a subcommand."""
    ctx = click.Context(click.Command("x"))
    ctx.obj = junk
    assert AmzContext.current(ctx) == AmzContext()


def test_current_picks_up_the_ambient_click_context():
    ctx = click.Context(click.Command("x"))
    ctx.obj = AmzContext.resolve(cache="30s")
    with ctx:
        assert AmzContext.current().cache_ttl == 30


# ---------------------------------------------------------------- to_amz_error

@pytest.mark.parametrize(
    "exc",
    [
        NotFoundError("gone"),
        BotCheckError(),
        RateLimitedError("slow down"),
        NetworkError("boom"),
        ParseError("markup changed"),
        InputError("bad asin"),
    ],
)
def test_a_typed_error_passes_through_untouched(exc):
    assert to_amz_error(exc) is exc


def test_a_value_error_becomes_an_input_error():
    """`validate_asin` predates the hierarchy and still raises ValueError."""
    converted = to_amz_error(ValueError("Invalid ASIN format: 'NOPE'"))
    assert isinstance(converted, InputError)
    assert converted.exit_code == 2
    assert "Invalid ASIN format" in str(converted)


def test_a_timeout_becomes_a_network_error():
    converted = to_amz_error(TimeoutError("too slow"))
    assert isinstance(converted, NetworkError)
    assert converted.exit_code == 3
    assert "timed out" in str(converted).lower()


def test_an_unknown_exception_becomes_a_network_error():
    converted = to_amz_error(RuntimeError("something odd"))
    assert isinstance(converted, NetworkError)
    assert str(converted) == "something odd"


def test_an_exception_with_no_message_still_says_something():
    converted = to_amz_error(RuntimeError())
    assert isinstance(converted, AmzError)
    assert str(converted) == "RuntimeError"


def test_every_error_type_keeps_its_documented_exit_code():
    """These codes are the CLI's contract with scripts -- they cannot drift."""
    assert AmzError.exit_code == 1
    assert InputError.exit_code == 2
    assert NetworkError.exit_code == 3
    assert NotFoundError.exit_code == 4
    assert BotCheckError.exit_code == 5
    assert RateLimitedError.exit_code == 5
    assert ParseError.exit_code == 6
