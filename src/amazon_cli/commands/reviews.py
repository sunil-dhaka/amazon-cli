"""amz reviews command."""

import asyncio

import click

from amazon_cli.client.product import get_reviews
from amazon_cli.context import AmzContext, to_amz_error
from amazon_cli.output import (
    error,
    output_csv,
    output_json,
    output_plain,
    print_reviews,
)

#: `body` is truncated here to keep a TSV line readable in a terminal.
PLAIN_HEADERS = ["rating", "title", "author", "date", "verified", "body"]

#: CSV quotes embedded newlines, so it carries the whole body.
CSV_HEADERS = ["rating", "title", "author", "date", "verified", "body"]


@click.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.pass_context
def reviews(ctx, asin, as_json, as_plain, as_csv):
    """Read product reviews (~13 top reviews from product page)."""
    settings = AmzContext.current(ctx)
    review_list = asyncio.run(_reviews(settings, asin))

    if not review_list:
        error("No reviews found.")
        return

    if as_json:
        output_json({
            "asin": asin,
            "reviews": [r.to_dict() for r in review_list],
        })
    elif as_plain:
        rows = [
            [r.rating, r.title, r.author, r.date, int(r.verified), r.body[:100]]
            for r in review_list
        ]
        output_plain(rows, PLAIN_HEADERS)
    elif as_csv:
        rows = [
            [r.rating, r.title, r.author, r.date, int(r.verified), r.body]
            for r in review_list
        ]
        output_csv(rows, CSV_HEADERS)
    else:
        print_reviews(review_list, asin)


async def _reviews(settings, asin):
    """Fetch reviews through the globally configured client.

    Building a bare ``AmazonClient()`` here -- as this did before -- silently
    threw away ``--timeout``, ``--retries``, ``--min-interval`` and the whole
    cache, so `amz reviews` was the one command the global flags did not reach.
    """
    async with settings.client() as client:
        try:
            return await get_reviews(client, asin)
        except ValueError as exc:
            # The client already raises typed errors; a bare ValueError comes
            # from `validate_asin` and is user input, which is exit 2, not 1.
            raise to_amz_error(exc) from exc
