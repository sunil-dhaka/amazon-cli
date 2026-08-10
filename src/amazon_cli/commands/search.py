"""amz search command."""

import asyncio

import click

from amazon_cli import money
from amazon_cli.client.search import SORT_OPTIONS, search_products
from amazon_cli.context import AmzContext, to_amz_error
from amazon_cli.output import (
    error,
    output_csv,
    output_json,
    output_plain,
    print_products_table,
)

PLAIN_HEADERS = ["asin", "title", "price", "rating", "reviews", "prime"]
CSV_HEADERS = ["asin", "title", "price", "price_paise", "rating", "reviews", "prime"]


@click.command()
@click.argument("query")
@click.option("--page", "-p", default=1, type=click.IntRange(min=1), help="Page number.")
@click.option(
    "--sort", "-s",
    type=click.Choice(list(SORT_OPTIONS.keys())),
    default="relevance",
    help="Sort order.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.pass_context
def search(ctx, query, page, sort, as_json, as_plain, as_csv):
    """Search for products on Amazon.in."""
    settings = AmzContext.current(ctx)
    products, total = asyncio.run(_search(settings, query, page, sort))

    if not products:
        error("No results found.")
        return

    if as_json:
        output_json({
            "total": total,
            "page": page,
            "products": [p.to_dict() for p in products],
        })
    elif as_plain:
        rows = [
            [p.asin, p.title, p.price, p.rating, p.review_count, int(p.is_prime)]
            for p in products
        ]
        output_plain(rows, PLAIN_HEADERS)
    elif as_csv:
        rows = [
            [
                p.asin, p.title, money.rupees(p.price), p.price,
                p.rating, p.review_count, int(p.is_prime),
            ]
            for p in products
        ]
        output_csv(rows, CSV_HEADERS)
    else:
        print_products_table(products, total_count=total, page=page)


async def _search(settings, query, page, sort):
    async with settings.client() as client:
        try:
            return await search_products(client, query, page=page, sort=sort)
        except ValueError as exc:
            # Typed errors from the client already carry an exit code; a bare
            # ValueError is bad user input and needs promoting to one.
            raise to_amz_error(exc) from exc
