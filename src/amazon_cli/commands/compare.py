"""amz compare command."""

import asyncio

import click

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.product import get_product
from amazon_cli.output import error, output_json, output_plain, print_compare_table


@click.command()
@click.argument("asins", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
def compare(asins, as_json, as_plain):
    """Compare products side by side (2+ ASINs)."""
    if len(asins) < 2:
        error("Provide at least 2 ASINs to compare.")
    asyncio.run(_compare(asins, as_json, as_plain))


async def _compare(asins, as_json, as_plain):
    async with AmazonClient() as client:
        try:
            products = await asyncio.gather(
                *(get_product(client, asin) for asin in asins)
            )
        except Exception as e:
            error(str(e))

    if as_json:
        output_json([p.to_dict() for p in products])
    elif as_plain:
        headers = ["asin", "title", "brand", "price", "mrp", "discount", "rating", "reviews"]
        rows = [
            [p.asin, p.title, p.brand, p.price, p.mrp, p.discount, p.rating, p.review_count]
            for p in products
        ]
        output_plain(rows, headers)
    else:
        print_compare_table(products)
