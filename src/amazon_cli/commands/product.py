"""amz product command."""

import asyncio

import click
import httpx

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.product import get_product
from amazon_cli.output import error, output_json, output_plain, print_product_detail


@click.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
def product(asin, as_json, as_plain):
    """View product details by ASIN."""
    asyncio.run(_product(asin, as_json, as_plain))


async def _product(asin, as_json, as_plain):
    async with AmazonClient() as client:
        try:
            detail = await get_product(client, asin)
        except (httpx.HTTPError, TimeoutError, ValueError) as e:
            error(str(e))
            return

    if as_json:
        output_json(detail.to_dict())
    elif as_plain:
        headers = ["asin", "title", "brand", "price", "mrp", "discount", "rating", "reviews", "stock"]
        rows = [[
            detail.asin, detail.title, detail.brand, detail.price,
            detail.mrp, detail.discount, detail.rating, detail.review_count,
            detail.availability,
        ]]
        output_plain(rows, headers)
    else:
        print_product_detail(detail)
