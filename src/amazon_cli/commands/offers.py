"""amz offers command."""

import asyncio

import click
from rich.table import Table

from amazon_cli import money
from amazon_cli.client.base import validate_asin
from amazon_cli.client.offers import get_offers, sort_offers
from amazon_cli.context import AmzContext, to_amz_error
from amazon_cli.output import console, output_csv, output_json, output_plain

PLAIN_HEADERS = ["price", "shipping", "total", "condition", "seller", "ships_from", "delivery"]

CSV_HEADERS = [
    "price", "price_paise", "shipping", "shipping_paise", "total", "total_paise",
    "condition", "seller", "ships_from", "delivery", "is_prime",
]


@click.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option("--limit", type=click.IntRange(min=1), default=None, metavar="N",
              help="Show at most N offers.")
@click.option("--new-only", is_flag=True, help="Hide used and renewed offers.")
@click.option("--sort", "sort_key", type=click.Choice(["total", "price", "rating"]),
              default="total", show_default=True,
              help="Order by. 'total' is price plus delivery -- the number that decides it.")
@click.pass_context
def offers(ctx, asin, as_json, as_plain, as_csv, limit, new_only, sort_key):
    """Show buying options for ASIN.

    Note: Amazon loads its full third-party seller list over AJAX, and that
    endpoint is not reachable without a session. What is server-rendered -- and
    therefore what this shows -- is the buy-box offer: the price you would
    actually pay, who sells it and who ships it.
    """
    settings = AmzContext.current(ctx)
    # Validated here so a malformed ASIN exits 2 (InputError) instead of
    # escaping as a bare ValueError traceback with exit 1.
    try:
        asin = validate_asin(asin)
    except ValueError as exc:
        raise to_amz_error(exc) from exc

    found = asyncio.run(_fetch(settings, asin))

    if new_only:
        found = [o for o in found if "used" not in o.condition.lower()
                 and "renewed" not in o.condition.lower()]
    found = sort_offers(found, sort_key)
    if limit:
        found = found[:limit]

    if as_json:
        output_json([o.to_dict() for o in found])
        return
    if as_plain:
        output_plain(
            [[o.price, o.shipping, o.total, o.condition, o.seller, o.ships_from, o.delivery]
             for o in found],
            PLAIN_HEADERS,
        )
        return
    if as_csv:
        output_csv(
            [[money.rupees(o.price), o.price, money.rupees(o.shipping), o.shipping,
              money.rupees(o.total), o.total, o.condition, o.seller, o.ships_from,
              o.delivery, o.is_prime] for o in found],
            CSV_HEADERS,
        )
        return

    if not found:
        console.print(f"[yellow]No buying options found for {asin.upper()}.[/]")
        return
    _render(found, asin)


async def _fetch(settings, asin):
    async with settings.client() as client:
        return await get_offers(client, asin)


def _render(found, asin) -> None:
    table = Table(title=f"Buying options -- {asin.upper()}", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Price", justify="right")
    table.add_column("Delivery", justify="right")
    table.add_column("Total", justify="right", style="bold")
    table.add_column("Condition", width=10)
    table.add_column("Sold by", max_width=28)
    table.add_column("Ships from", max_width=16)

    for i, offer in enumerate(found, 1):
        # The first row is the cheapest by total; mark it so the eye lands there.
        style = "bold green" if i == 1 and len(found) > 1 else None
        table.add_row(
            str(i),
            money.format_inr(offer.price),
            money.format_inr(offer.shipping) if offer.shipping else "free",
            money.format_inr(offer.total),
            offer.condition or "-",
            offer.seller or "-",
            offer.ships_from or "-",
            style=style,
        )
    console.print(table)

    if len(found) == 1:
        console.print(
            "[dim]Only the buy-box offer is server-rendered; Amazon's full "
            "seller list is loaded over AJAX and is not available here.[/]"
        )
