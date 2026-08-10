"""amz bestsellers command."""

import asyncio

import click
from rich.table import Table

from amazon_cli import money
from amazon_cli.client.deals import (
    BESTSELLER_CATEGORIES,
    get_bestsellers,
    resolve_category,
    sort_bestsellers,
)
from amazon_cli.context import AmzContext
from amazon_cli.output import console, output_csv, output_json, output_plain

PLAIN_HEADERS = ["rank", "asin", "title", "price", "rating", "reviews"]

CSV_HEADERS = ["rank", "asin", "title", "price", "price_paise", "rating", "reviews", "category"]


@click.command()
@click.argument("category", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option("--limit", type=click.IntRange(min=1), default=25, show_default=True,
              metavar="N", help="Show at most N items.")
@click.option("--list-categories", is_flag=True, help="List the known category slugs and exit.")
@click.pass_context
def bestsellers(ctx, category, as_json, as_plain, as_csv, limit, list_categories):
    """Amazon.in bestsellers, optionally for one CATEGORY.

    Run with --list-categories to see the valid slugs. An unknown slug is
    rejected with the closest matches rather than a silent empty list.
    """
    if list_categories:
        _print_categories()
        return

    # Resolve up front so a typo costs nothing: an unknown slug fails here,
    # before a client is built, and the label below names the department we
    # actually fetched rather than whatever the user typed ("home" -> kitchen).
    slug = resolve_category(category)

    settings = AmzContext.current(ctx)
    items = sort_bestsellers(asyncio.run(_fetch(settings, slug)), limit=limit)
    label = BESTSELLER_CATEGORIES.get(slug or "", "All departments")

    if as_json:
        output_json([i.to_dict() for i in items])
        return
    if as_plain:
        output_plain(
            [[i.rank, i.asin, i.title, i.price, i.rating, i.review_count] for i in items],
            PLAIN_HEADERS,
        )
        return
    if as_csv:
        output_csv(
            [[i.rank, i.asin, i.title, money.rupees(i.price), i.price, i.rating,
              i.review_count, label] for i in items],
            CSV_HEADERS,
        )
        return

    if not items:
        console.print(
            f"[yellow]No bestsellers parsed for {label}.[/] "
            "Amazon may have changed this page's markup."
        )
        return
    _render(items, label)


async def _fetch(settings, category):
    async with settings.client() as client:
        return await get_bestsellers(client, category)


def _print_categories() -> None:
    table = Table(title="Bestseller categories", show_lines=False)
    table.add_column("Slug", style="cyan")
    table.add_column("Department")
    for slug, name in sorted(BESTSELLER_CATEGORIES.items()):
        table.add_row(slug, name)
    console.print(table)
    console.print("\n[dim]Example:[/] amz bestsellers electronics --limit 10")


def _render(items, label) -> None:
    table = Table(title=f"Bestsellers -- {label}", show_lines=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("ASIN", style="cyan", width=12)
    table.add_column("Title", max_width=48)
    table.add_column("Price", justify="right")
    table.add_column("Rating", justify="center", width=12)

    for item in items:
        rating = f"{item.rating:.1f} ({item.review_count:,})" if item.rating else "-"
        table.add_row(
            str(item.rank or "-"),
            item.asin,
            item.title[:48],
            money.format_inr(item.price) if item.price else "-",
            rating,
        )
    console.print(table)
