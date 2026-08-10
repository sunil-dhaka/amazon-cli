"""amz cache command group -- inspect and clear the on-disk response cache."""

import click

from amazon_cli.context import AmzContext

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(num: int) -> str:
    """``3_500_000 -> '3.3 MB'``. Binary units, one decimal above kilobytes."""
    size = float(num)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"  # pragma: no cover -- loop always returns


@click.group("cache")
def cache():
    """Inspect and clear the on-disk response cache."""


@cache.command("stats")
@click.pass_context
def cache_stats(ctx):
    """Show how many entries the cache holds and how much disk they use."""
    settings = AmzContext.current(ctx)
    count, size = settings.cache().stats()
    click.echo(f"Entries:   {count}")
    click.echo(f"Size:      {human_size(size)}")
    click.echo(f"Directory: {settings.cache_directory}")


@cache.command("clear")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def cache_clear(ctx, yes):
    """Delete every cached response."""
    settings = AmzContext.current(ctx)
    directory = settings.cache_directory
    count, _ = settings.cache().stats()

    if not count:
        click.echo(f"Cache is already empty ({directory}).")
        return

    if not yes:
        click.confirm(f"Delete {count} cached entries from {directory}?", abort=True)

    removed = settings.cache().clear()
    click.echo(f"Removed {removed} cached {'entry' if removed == 1 else 'entries'}.")


@cache.command("path")
@click.pass_context
def cache_path(ctx):
    """Print the cache directory."""
    click.echo(str(AmzContext.current(ctx).cache_directory))
