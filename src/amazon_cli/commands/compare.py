"""amz compare command."""

import asyncio

import click

from amazon_cli import money
from amazon_cli.client.product import get_product
from amazon_cli.context import DEFAULT_CONCURRENCY, AmzContext, to_amz_error
from amazon_cli.output import (
    err_console,
    error,
    output_csv,
    output_json,
    output_plain,
    print_compare_table,
)

PLAIN_HEADERS = ["asin", "title", "brand", "price", "mrp", "discount", "rating", "reviews"]
CSV_HEADERS = [
    "asin", "title", "brand", "price", "price_paise", "mrp", "mrp_paise",
    "discount", "discount_percent", "rating", "reviews",
]


@click.command()
@click.argument("asins", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    metavar="N",
    help="Maximum simultaneous fetches.",
)
@click.pass_context
def compare(ctx, asins, as_json, as_plain, as_csv, concurrency):
    """Compare products side by side (2+ ASINs)."""
    if len(asins) < 2:
        error("Provide at least 2 ASINs to compare.", 2)
        return

    settings = AmzContext.current(ctx)
    products = asyncio.run(_compare(settings, asins, concurrency))

    if not products:
        error("All product lookups failed.")
        return

    if as_json:
        output_json([p.to_dict() for p in products])
    elif as_plain:
        rows = [
            [p.asin, p.title, p.brand, p.price, p.mrp, p.discount, p.rating, p.review_count]
            for p in products
        ]
        output_plain(rows, PLAIN_HEADERS)
    elif as_csv:
        rows = [
            [
                p.asin, p.title, p.brand,
                money.rupees(p.price), p.price,
                money.rupees(p.mrp), p.mrp,
                p.discount, p.discount_pct, p.rating, p.review_count,
            ]
            for p in products
        ]
        output_csv(rows, CSV_HEADERS)
    else:
        print_compare_table(products)


async def _compare(settings, asins, concurrency):
    limit = asyncio.Semaphore(max(1, concurrency))

    async with settings.client() as client:

        async def one(asin):
            async with limit:
                return await get_product(client, asin)

        results = await asyncio.gather(
            *(one(asin) for asin in asins), return_exceptions=True
        )

    products = []
    for asin, result in zip(asins, results):
        if isinstance(result, BaseException):
            err_console.print(
                f"[yellow]Warning:[/] Failed to fetch {asin}: {to_amz_error(result)}"
            )
        else:
            products.append(result)
    return products
