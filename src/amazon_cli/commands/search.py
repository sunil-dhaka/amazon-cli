"""amz search command."""

import asyncio

import click

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.search import SORT_OPTIONS, search_products
from amazon_cli.output import error, output_json, output_plain, print_products_table


@click.command()
@click.argument("query")
@click.option("--page", "-p", default=1, help="Page number.")
@click.option(
    "--sort",
    "-s",
    type=click.Choice(list(SORT_OPTIONS.keys())),
    default="relevance",
    help="Sort order.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
def search(query, page, sort, as_json, as_plain):
    """Search for products on Amazon.in."""
    asyncio.run(_search(query, page, sort, as_json, as_plain))


async def _search(query, page, sort, as_json, as_plain):
    async with AmazonClient() as client:
        try:
            products, total = await search_products(client, query, page=page, sort=sort)
        except Exception as e:
            error(str(e))

    if not products:
        error("No results found.")

    if as_json:
        output_json({
            "total": total,
            "page": page,
            "products": [p.to_dict() for p in products],
        })
    elif as_plain:
        headers = ["asin", "title", "price", "rating", "reviews", "prime"]
        rows = [
            [p.asin, p.title, p.price, p.rating, p.review_count, p.is_prime]
            for p in products
        ]
        output_plain(rows, headers)
    else:
        print_products_table(products, total_count=total, page=page)
