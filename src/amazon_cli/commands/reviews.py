"""amz reviews command."""

import asyncio

import click
import httpx

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.product import get_reviews
from amazon_cli.output import error, output_json, output_plain, print_reviews


@click.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
def reviews(asin, as_json, as_plain):
    """Read product reviews (~13 top reviews from product page)."""
    asyncio.run(_reviews(asin, as_json, as_plain))


async def _reviews(asin, as_json, as_plain):
    async with AmazonClient() as client:
        try:
            review_list = await get_reviews(client, asin)
        except (httpx.HTTPError, TimeoutError, ValueError) as e:
            error(str(e))
            return

    if not review_list:
        error("No reviews found.")
        return

    if as_json:
        output_json({
            "asin": asin,
            "reviews": [r.to_dict() for r in review_list],
        })
    elif as_plain:
        headers = ["rating", "title", "author", "date", "verified", "body"]
        rows = [
            [r.rating, r.title, r.author, r.date, int(r.verified), r.body[:100]]
            for r in review_list
        ]
        output_plain(rows, headers)
    else:
        print_reviews(review_list, asin)
