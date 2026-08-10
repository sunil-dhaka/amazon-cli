"""``amz watch`` -- a price watchlist that lives in your terminal.

The rules live in :mod:`amazon_cli.watch`; this module is only plumbing and
pixels. Every subcommand converts an :class:`~amazon_cli.errors.AmzError` into a
stderr line and that error's exit code, so scripts can tell a wrong ASIN (4)
from a bot check (5) without grepping our output.
"""

import asyncio
import functools
import time

import click
from rich.table import Table
from rich.text import Text

from amazon_cli import money
from amazon_cli.client.base import AmazonClient
from amazon_cli.errors import AmzError, InputError, NotFoundError
from amazon_cli.context import AmzContext
from amazon_cli.output import console, error, output_csv, output_json, output_plain
from amazon_cli.watch.service import (
    MIN_INTERVAL,
    SORT_KEYS,
    check_all,
    normalize_asin,
    parse_target_paise,
    seed_product,
    sort_entries,
    sparkline,
    time_weighted_average,
)
from amazon_cli.watch.store import WatchStore

#: Sparkline width inside the `list` table; `history` gets the wider one.
_LIST_SPARK = 12
_HISTORY_SPARK = 48


def _handle_errors(func):
    """Turn a raised :class:`AmzError` into ``error(msg, exit_code)``."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AmzError as exc:
            error(str(exc), exc.exit_code)

    return wrapper


def _timestamp(value: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) if value else "never"


def _short(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "..."


@click.group()
def watch():
    """Track prices and get told when they drop.

    Add a product with a target price, then run `amz watch check` (from cron,
    say) to re-fetch everything and print only what crossed the line.
    """


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@watch.command()
@click.argument("asin")
@click.option("--target", required=True, help="Alert when the price is at or below this, in rupees.")
@click.option("--note", default="", help="Free-text reminder shown next to the product.")
@click.pass_context
@_handle_errors
def add(ctx, asin, target, note):
    """Watch ASIN and alert at or below TARGET rupees."""
    settings = AmzContext.current(ctx)
    asin = normalize_asin(asin)
    target_paise = parse_target_paise(target)

    with WatchStore() as store:
        entry, created = store.add(asin, target_paise=target_paise, note=note)
        result = asyncio.run(_seed(store, asin, settings))

        if not result.ok and result.exit_code == NotFoundError.exit_code:
            # Amazon says the product does not exist -- do not leave a phantom
            # row behind that will fail on every future check.
            if created:
                store.remove(asin)
            raise NotFoundError(result.error)

        entry = store.require(asin)
        verb = "Watching" if created else "Updated"
        title = _short(entry.title or "(title unknown)", 60)
        console.print(f"[bold green]{verb}[/] [cyan]{asin}[/] {title}")
        console.print(f"  target [bold]{money.format_inr(entry.target_paise)}[/]", end="")
        if entry.current_paise:
            console.print(f"   now {money.format_inr(entry.current_paise)}", end="")
            if entry.below_target:
                console.print("   [bold green](already at or below target)[/]", end="")
            else:
                console.print(f"   [dim]{money.format_inr(entry.gap_paise)} to go[/]", end="")
        console.print()
        if not result.ok:
            console.print(f"  [yellow]Could not fetch the price right now:[/] {result.error}")


async def _seed(store, asin, settings):
    async with _client(settings) as client:
        return await seed_product(store, asin, client=client)

def _client(settings):
    """A client built from the global options, with watch's own politeness floor.

    `watch` sweeps the whole list in one go, so it never goes faster than
    MIN_INTERVAL even if the user asked for less -- but --timeout, --retries and
    --cache were previously dropped on the floor here, which meant `amz --cache
    10m watch check` quietly refetched every page.
    """
    return AmazonClient(
        timeout=settings.timeout,
        cache=settings.cache(),
        max_retries=settings.retries,
        min_interval=max(settings.min_interval, MIN_INTERVAL),
    )



# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@watch.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option(
    "--sort",
    "sort_key",
    default="added",
    help=f"Sort by one of: {', '.join(SORT_KEYS)}.",
)
@_handle_errors
def list_cmd(as_json, as_plain, as_csv, sort_key):
    """Show the watchlist."""
    with WatchStore() as store:
        entries = sort_entries(store.list_all(), sort_key)
        histories = store.all_histories() if entries else {}

    if as_json:
        output_json([_entry_json(e, histories) for e in entries])
        return

    headers = [
        "asin", "title", "price", "price_paise", "target", "target_paise",
        "lowest_paise", "highest_paise", "drop_percent", "below_target", "muted", "note",
    ]
    rows = [
        [
            e.asin, e.title, money.rupees(e.current_paise), e.current_paise,
            money.rupees(e.target_paise), e.target_paise, e.lowest_paise, e.highest_paise,
            e.drop_percent, e.below_target, not e.alerts_enabled, e.note,
        ]
        for e in entries
    ]
    if as_csv:
        output_csv(rows, headers)
        return
    if as_plain:
        output_plain(rows, headers)
        return

    if not entries:
        console.print("[dim]Nothing on the watchlist yet.[/]")
        console.print("[dim]Add one with:[/] amz watch add B0BZP2H373 --target 24990")
        return
    console.print(_list_table(entries, histories))


def _entry_json(entry, histories) -> dict:
    data = entry.to_dict()
    data["points"] = len(histories.get(entry.asin, []))
    data["sparkline"] = sparkline(histories.get(entry.asin, []), _LIST_SPARK)
    return data


def _list_table(entries, histories) -> Table:
    table = Table(title=f"Watchlist ({len(entries)})", show_lines=False)
    table.add_column("ASIN", style="cyan", no_wrap=True)
    table.add_column("Title", max_width=34)
    table.add_column("Price", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Low", justify="right")
    table.add_column("High", justify="right")
    table.add_column("Trend", no_wrap=True)
    table.add_column("Status")

    for entry in entries:
        price = Text(money.format_inr(entry.current_paise))
        price.stylize("bold green" if entry.below_target else "bold")

        status = Text()
        if entry.below_target:
            status.append("HIT", style="bold green")
        elif entry.current_paise:
            status.append(f"{money.format_inr(entry.gap_paise)} to go", style="dim")
        else:
            status.append("no price", style="dim")
        if not entry.alerts_enabled:
            status.append("  muted", style="yellow")
        if entry.last_error:
            status.append("  error", style="red")

        table.add_row(
            entry.asin,
            _short(entry.title or "(unknown)", 34),
            price,
            money.format_inr(entry.target_paise),
            money.format_inr(entry.lowest_paise),
            money.format_inr(entry.highest_paise),
            sparkline(histories.get(entry.asin, []), _LIST_SPARK),
            status,
        )
    return table


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@watch.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--quiet", "-q", is_flag=True, help="Print alerts only (for cron).")
@click.option("--only", multiple=True, metavar="ASIN", help="Check just this ASIN; repeatable.")
@click.pass_context
@_handle_errors
def check(ctx, as_json, quiet, only):
    """Re-fetch every watched product and report the hits.

    Polite by design: one product at a time with a randomised 2-6s gap. Exits 0
    even when some products failed -- a single flaky page should not fail your
    cron job -- and non-zero only when every one of them failed.
    """
    settings = AmzContext.current(ctx)
    wanted = [normalize_asin(a) for a in only]

    with WatchStore() as store:
        known = {e.asin for e in store.list_all()}
        if not known:
            if as_json:
                output_json({"checked": 0, "alerts": 0, "failed": 0, "results": []})
            elif not quiet:
                console.print("[dim]Nothing on the watchlist yet.[/]")
            return

        missing = [a for a in wanted if a not in known]
        if missing:
            raise NotFoundError(f"Not on your watchlist: {', '.join(missing)}")

        results = asyncio.run(_check(store, wanted, settings))

    alerts = [r for r in results if r.alerted]
    failed = [r for r in results if not r.ok]

    if as_json:
        output_json(
            {
                "checked": len(results),
                "alerts": len(alerts),
                "failed": len(failed),
                "results": [r.to_dict() for r in results],
            }
        )
    else:
        _print_check(results, alerts, failed, quiet)

    if results and len(failed) == len(results):
        error(
            f"All {len(failed)} checks failed. First error: {failed[0].error}",
            failed[0].exit_code,
        )


async def _check(store, wanted, settings):
    async with _client(settings) as client:
        return await check_all(store, wanted or None, client=client)


def _print_check(results, alerts, failed, quiet) -> None:
    for result in results:
        if result.alerted:
            line = Text()
            line.append("ALERT ", style="bold green")
            line.append(f"{result.asin} ", style="cyan")
            line.append(money.format_inr(result.current_paise), style="bold green")
            line.append(f" <= target {money.format_inr(result.target_paise)}")
            if result.previous_paise and result.previous_paise != result.current_paise:
                line.append(
                    f"  (was {money.format_inr(result.previous_paise)}, "
                    f"{result.change_percent:+d}%)",
                    style="dim",
                )
            line.append(f"  {_short(result.title, 44)}", style="dim")
            console.print(line)
        elif not quiet:
            if not result.ok:
                console.print(f"[red]FAIL [/] [cyan]{result.asin}[/] {result.error}")
            elif not result.current_paise:
                console.print(
                    f"[dim]--   [/] [cyan]{result.asin}[/] no price "
                    f"[dim]{result.availability or 'unavailable'}[/]"
                )
            else:
                delta = ""
                if result.previous_paise and result.changed_paise:
                    style = "green" if result.changed_paise < 0 else "red"
                    delta = f" [{style}]({result.change_percent:+d}%)[/]"
                console.print(
                    f"[dim]ok   [/] [cyan]{result.asin}[/] "
                    f"{money.format_inr(result.current_paise)}{delta} "
                    f"[dim]target {money.format_inr(result.target_paise)} "
                    f"-- {_short(result.title, 40)}[/]"
                )

    if quiet:
        return
    summary = f"{len(results)} checked, {len(alerts)} alert(s)"
    if failed:
        summary += f", {len(failed)} failed"
    console.print(f"[dim]{summary}[/]")


# ---------------------------------------------------------------------------
# remove / set-target / mute / unmute / clear
# ---------------------------------------------------------------------------


@watch.command()
@click.argument("asin")
@_handle_errors
def remove(asin):
    """Stop watching ASIN and delete its history."""
    asin = normalize_asin(asin)
    with WatchStore() as store:
        entry = store.require(asin)
        store.remove(asin)
    console.print(f"[green]Removed[/] [cyan]{asin}[/] {_short(entry.title, 50)}")


@watch.command("set-target")
@click.argument("asin")
@click.argument("target")
@_handle_errors
def set_target(asin, target):
    """Change the target price for ASIN to TARGET rupees."""
    asin = normalize_asin(asin)
    target_paise = parse_target_paise(target)
    with WatchStore() as store:
        before = store.require(asin)
        entry = store.set_target(asin, target_paise)
    console.print(
        f"[green]Target[/] [cyan]{asin}[/] "
        f"{money.format_inr(before.target_paise)} -> [bold]{money.format_inr(entry.target_paise)}[/]"
    )


@watch.command()
@click.argument("asin")
@_handle_errors
def mute(asin):
    """Keep tracking ASIN but stop alerting on it."""
    asin = normalize_asin(asin)
    with WatchStore() as store:
        store.set_alerts(asin, False)
    console.print(f"[yellow]Muted[/] [cyan]{asin}[/] [dim]-- still tracked, never alerts[/]")


@watch.command()
@click.argument("asin")
@_handle_errors
def unmute(asin):
    """Resume alerts for ASIN."""
    asin = normalize_asin(asin)
    with WatchStore() as store:
        store.set_alerts(asin, True)
    console.print(f"[green]Unmuted[/] [cyan]{asin}[/]")


@watch.command()
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@_handle_errors
def clear(yes):
    """Delete the entire watchlist."""
    with WatchStore() as store:
        total = store.count()
        if not total:
            console.print("[dim]Nothing on the watchlist yet.[/]")
            return
        if not yes:
            click.confirm(
                f"Delete all {total} watched product(s) and their price history?",
                abort=True,
            )
        removed = store.clear()
    console.print(f"[green]Cleared[/] {removed} product(s).")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@watch.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@_handle_errors
def history(asin, as_json):
    """Show the recorded price history for ASIN."""
    asin = normalize_asin(asin)
    with WatchStore() as store:
        entry = store.require(asin)
        points = store.history(asin)

    prices = [p.paise for p in points]
    average = time_weighted_average(points)

    if as_json:
        output_json(
            {
                "asin": entry.asin,
                "title": entry.title,
                "target": money.rupees(entry.target_paise),
                "target_paise": entry.target_paise,
                "price": money.rupees(entry.current_paise),
                "price_paise": entry.current_paise,
                "lowest_paise": entry.lowest_paise,
                "highest_paise": entry.highest_paise,
                "average_paise": average,
                "average": money.rupees(average),
                "sparkline": sparkline(prices, _HISTORY_SPARK),
                "points": [p.to_dict() for p in points],
            }
        )
        return

    console.print(f"[cyan]{entry.asin}[/] [bold]{_short(entry.title or '(unknown)', 60)}[/]")
    if not points:
        console.print("[dim]No price recorded yet. Run: amz watch check[/]")
        return

    console.print(f"  {sparkline(prices, _HISTORY_SPARK)}")
    console.print(
        f"  low [green]{money.format_inr(entry.lowest_paise)}[/]"
        f"   high [red]{money.format_inr(entry.highest_paise)}[/]"
        f"   avg {money.format_inr(average)}"
        f"   target {money.format_inr(entry.target_paise)}"
    )

    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("When", style="dim")
    table.add_column("Price", justify="right")
    table.add_column("Change", justify="right")

    previous = 0
    for point in points:
        change = Text("-", style="dim")
        if previous:
            pct = money.change_percent(previous, point.paise)
            change = Text(f"{pct:+d}%", style="green" if point.paise < previous else "red")
        table.add_row(_timestamp(point.recorded_at), money.format_inr(point.paise), change)
        previous = point.paise
    console.print(table)
