"""Resolved global CLI settings.

Every global option (`--cache`, `--timeout`, `--retries`, `--min-interval`)
lands here once, on click's context object, so a subcommand never has to know
how the flags were spelled -- it just asks for a configured client. Before this
existed each command built a bare ``AmazonClient()`` and the global flags had
nowhere to go.
"""

from dataclasses import dataclass
from pathlib import Path

import click

from amazon_cli.cache import ResponseCache, default_cache_dir, parse_duration
from amazon_cli.client.base import AmazonClient
from amazon_cli.errors import AmzError, InputError, NetworkError

#: Defaults for the global options, shared with the click decorators in cli.py.
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
DEFAULT_MIN_INTERVAL = 0.0

#: How many product pages a batch command fetches at once. Four is polite
#: enough that Amazon does not start serving bot checks, and still ~4x faster
#: than serial for a `amz product A B C D` style call.
DEFAULT_CONCURRENCY = 4


def to_amz_error(exc: BaseException) -> AmzError:
    """Coerce any fetch failure into a typed :class:`AmzError`.

    Lives here rather than in :mod:`amazon_cli.errors` because it is a CLI-layer
    concern: the client already raises typed errors, but ``validate_asin``
    predates the hierarchy and still raises ``ValueError`` for a malformed ASIN,
    which is squarely user input (exit 2).
    """
    if isinstance(exc, AmzError):
        return exc
    if isinstance(exc, ValueError):
        return InputError(str(exc))
    if isinstance(exc, TimeoutError):
        return NetworkError(f"Request timed out: {exc}")
    return NetworkError(str(exc) or exc.__class__.__name__)


@dataclass
class AmzContext:
    """The global options, already parsed and validated."""

    cache_ttl: int = 0
    """Cache lifetime in seconds. Zero disables the cache entirely."""

    cache_dir: Path | None = None
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    min_interval: float = DEFAULT_MIN_INTERVAL
    debug: bool = False

    @classmethod
    def resolve(
        cls,
        cache: str | None = None,
        no_cache: bool = False,
        cache_dir: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        debug: bool = False,
    ) -> "AmzContext":
        """Build from raw option values, raising :class:`InputError` on garbage.

        ``--no-cache`` wins over ``--cache`` so a shell alias carrying
        ``--cache 10m`` can be overridden on the command line without editing it.
        """
        ttl = 0
        if cache is not None and not no_cache:
            try:
                ttl = parse_duration(cache)
            except ValueError as exc:
                raise InputError(str(exc)) from exc

        if timeout <= 0:
            raise InputError("--timeout must be greater than 0.")
        if retries < 0:
            raise InputError("--retries cannot be negative.")
        if min_interval < 0:
            raise InputError("--min-interval cannot be negative.")

        return cls(
            cache_ttl=ttl,
            cache_dir=Path(cache_dir) if cache_dir else None,
            timeout=float(timeout),
            retries=int(retries),
            min_interval=float(min_interval),
            debug=bool(debug),
        )

    @classmethod
    def current(cls, ctx: click.Context | None = None) -> "AmzContext":
        """The settings on click's context, or defaults when there are none.

        A subcommand invoked directly (``CliRunner().invoke(product, ...)``)
        never runs the group callback, so ``ctx.obj`` is ``None``. Falling back
        to defaults keeps those call sites working instead of crashing.
        """
        if ctx is None:
            ctx = click.get_current_context(silent=True)
        obj = getattr(ctx, "obj", None) if ctx is not None else None
        return obj if isinstance(obj, cls) else cls()

    @property
    def cache_directory(self) -> Path:
        return self.cache_dir or default_cache_dir()

    def cache(self) -> ResponseCache:
        """A cache honouring `--cache`/`--no-cache`/`--cache-dir`."""
        return ResponseCache(self.cache_ttl, self.cache_directory)

    def client(self) -> AmazonClient:
        """An :class:`AmazonClient` configured from the global options."""
        return AmazonClient(
            timeout=self.timeout,
            cache=self.cache(),
            max_retries=self.retries,
            min_interval=self.min_interval,
        )
