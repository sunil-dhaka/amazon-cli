"""amz reviews command."""

import asyncio

import click

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.product import get_reviews
from amazon_cli.output import error, output_json, output_plain, print_reviews


@click.command()
@click.argument("asin")
@click.option("--page", "-p", default=1, help="Page number.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
def reviews(asin, page, as_json, as_plain):
    """Read product reviews."""
    asyncio.run(_reviews(asin, page, as_json, as_plain))


async def _reviews(asin, page, as_json, as_plain):
    async with AmazonClient() as client:
        try:
            review_list = await get_reviews(client, asin, page=page)
        except Exception as e:
            error(str(e))

    if not review_list:
        error("No reviews found.")

    if as_json:
        output_json({
            "asin": asin,
            "page": page,
            "reviews": [r.to_dict() for r in review_list],
        })
    elif as_plain:
        headers = ["rating", "title", "author", "date", "verified", "body"]
        rows = [
            [r.rating, r.title, r.author, r.date, r.verified, r.body[:100]]
            for r in review_list
        ]
        output_plain(rows, headers)
    else:
        print_reviews(review_list, asin, page=page)
