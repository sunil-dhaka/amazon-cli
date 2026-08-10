"""CLI entry point -- click group and global options."""

import sys
from pathlib import Path

import click

from amazon_cli import __version__
from amazon_cli.commands.cache import cache
from amazon_cli.commands.bestsellers import bestsellers
from amazon_cli.commands.compare import compare
from amazon_cli.commands.deals import deals
from amazon_cli.commands.completions import completions
from amazon_cli.commands.offers import offers
from amazon_cli.commands.product import product
from amazon_cli.commands.reviews import reviews
from amazon_cli.commands.search import search
from amazon_cli.commands.variants import variants
from amazon_cli.context import (
    DEFAULT_MIN_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    AmzContext,
)
from amazon_cli.errors import AmzError
from amazon_cli.output import err_console
from amazon_cli.commands.watch import watch


class AmzGroup(click.Group):
    """Group that turns an :class:`AmzError` into a one-line message.

    A scraper fails constantly and for boring reasons -- a wrong ASIN, a bot
    check, a flaky network. A Python traceback for any of those is noise, and it
    hides the one thing a script needs: the exit code. `--debug` re-raises so a
    traceback is still one flag away while developing.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except AmzError as exc:
            if ctx.params.get("debug"):
                raise
            err_console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(exc.exit_code)


@click.group(cls=AmzGroup)
@click.version_option(__version__, prog_name="amz")
@click.option(
    "--cache",
    "cache_ttl",
    metavar="DURATION",
    default=None,
    help="Cache responses for this long (30s, 10m, 2h, 1d; bare number = minutes; max 365d).",
)
@click.option("--no-cache", is_flag=True, help="Disable the cache (overrides --cache).")
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Cache directory [default: $XDG_CACHE_HOME/amz or ~/.cache/amz].",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=DEFAULT_TIMEOUT,
    show_default=True,
    metavar="SECONDS",
    help="Per-request timeout.",
)
@click.option(
    "--retries",
    type=click.IntRange(min=0),
    default=DEFAULT_RETRIES,
    show_default=True,
    metavar="N",
    help="Retries after a throttle or transient network failure.",
)
@click.option(
    "--min-interval",
    type=click.FloatRange(min=0),
    default=DEFAULT_MIN_INTERVAL,
    show_default=True,
    metavar="SECONDS",
    help="Minimum gap between requests (politeness throttle).",
)
@click.option("--debug", is_flag=True, help="Re-raise errors with a full traceback.")
@click.pass_context
def cli(ctx, cache_ttl, no_cache, cache_dir, timeout, retries, min_interval, debug):
    """amz -- Amazon.in in your terminal."""
    ctx.obj = AmzContext.resolve(
        cache=cache_ttl,
        no_cache=no_cache,
        cache_dir=cache_dir,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        debug=debug,
    )


cli.add_command(search)
cli.add_command(product)
cli.add_command(compare)
cli.add_command(reviews)
cli.add_command(cache)
cli.add_command(completions)
cli.add_command(watch)
cli.add_command(offers)
cli.add_command(variants)
cli.add_command(deals)
cli.add_command(bestsellers)
