"""amz deals command."""

import asyncio

import click
from rich.table import Table

from amazon_cli import money
from amazon_cli.client.deals import deal_discount, filter_deals, get_deals
from amazon_cli.context import AmzContext
from amazon_cli.output import console, output_csv, output_json, output_plain

PLAIN_HEADERS = ["asin", "title", "price", "mrp", "discount_percent", "rating", "reviews"]

CSV_HEADERS = [
    "asin", "title", "price", "price_paise", "mrp", "mrp_paise",
    "discount", "discount_percent", "rating", "reviews", "badge",
]


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option("--limit", type=click.IntRange(min=1), default=25, show_default=True,
              metavar="N", help="Show at most N deals.")
@click.option("--min-discount", type=click.IntRange(0, 100), default=0, metavar="PCT",
              help="Only deals discounted at least this much.")
@click.pass_context
def deals(ctx, as_json, as_plain, as_csv, limit, min_discount):
    """Today's deals on Amazon.in, best discount first."""
    settings = AmzContext.current(ctx)
    found = asyncio.run(_fetch(settings))
    shown = filter_deals(found, min_discount=min_discount, limit=limit)

    if as_json:
        output_json([d.to_dict() for d in shown])
        return
    if as_plain:
        output_plain(
            [[d.asin, d.title, d.price, d.mrp, d.discount_percent, d.rating, d.review_count]
             for d in shown],
            PLAIN_HEADERS,
        )
        return
    if as_csv:
        output_csv(
            [[d.asin, d.title, money.rupees(d.price), d.price, money.rupees(d.mrp), d.mrp,
              d.discount, d.discount_percent, d.rating, d.review_count, d.badge]
             for d in shown],
            CSV_HEADERS,
        )
        return

    if not shown:
        _explain_empty(found, min_discount)
        return
    _render(shown, found)


async def _fetch(settings):
    async with settings.client() as client:
        return await get_deals(client)


def _explain_empty(found, min_discount) -> None:
    """Say *why* the table is empty. A bare blank table is a bug report waiting."""
    if found and min_discount:
        # deal_discount, not discount_percent: it is the number --min-discount
        # actually filtered on, so "best right now" can never contradict the
        # filter that just rejected everything.
        best = max((deal_discount(d) for d in found), default=0)
        console.print(
            f"[yellow]No deals at {min_discount}% or more.[/] "
            f"Best right now is {best}% across {len(found)} deals."
        )
    else:
        console.print(
            "[yellow]No deals found.[/] Amazon renders much of its deals page "
            "client-side, so the server HTML sometimes carries nothing."
        )


def _render(shown, found) -> None:
    table = Table(title=f"Today's Deals ({len(shown)} of {len(found)})", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("ASIN", style="cyan", width=12)
    table.add_column("Title", max_width=44)
    table.add_column("Price", justify="right")
    table.add_column("MRP", justify="right", style="dim")
    table.add_column("Off", justify="right", width=5)

    for i, deal in enumerate(shown, 1):
        off = f"{deal.discount_percent}%" if deal.discount_percent else "-"
        table.add_row(
            str(i),
            deal.asin,
            deal.title[:44],
            money.format_inr(deal.price),
            money.format_inr(deal.mrp) if deal.mrp else "-",
            off,
        )
    console.print(table)
