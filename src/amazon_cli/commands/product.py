"""amz product command -- one or many ASINs, fetched concurrently."""

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
    print_product_detail,
)

#: Kept exactly as it was before batch support: `price` here is paise, which is
#: wrong-looking but is what every existing `--plain` consumer already parses.
PLAIN_HEADERS = [
    "asin", "title", "brand", "price", "mrp", "discount", "rating", "reviews", "stock",
]

#: CSV is new, so it carries both forms: rupees for humans, paise for maths.
CSV_HEADERS = [
    "asin", "title", "brand", "price", "price_paise", "mrp", "mrp_paise",
    "discount", "discount_percent", "rating", "reviews", "stock",
]


@click.command()
@click.argument("asins", nargs=-1, required=True, metavar="ASIN...")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    metavar="N",
    help="Maximum simultaneous fetches when several ASINs are given.",
)
@click.pass_context
def product(ctx, asins, as_json, as_plain, as_csv, concurrency):
    """View product details by ASIN.

    Accepts several ASINs, which are fetched concurrently. One bad ASIN does not
    sink the batch: it is reported on stderr and the rest are still printed.
    """
    settings = AmzContext.current(ctx)
    details, failures = asyncio.run(_fetch_all(settings, asins, concurrency))

    if not details:
        _report_total_failure(failures)
        return

    for asin, exc in failures:
        err_console.print(f"[yellow]Warning:[/] {asin}: {exc}")

    _emit(details, single=len(asins) == 1, as_json=as_json, as_plain=as_plain, as_csv=as_csv)


async def _fetch_all(settings, asins, concurrency):
    """Fetch every ASIN, bounded by a semaphore. Never raises for one bad ASIN.

    ``return_exceptions=True`` is what keeps a batch alive: without it the first
    failure cancels the siblings and the user loses work already paid for.
    """
    limit = asyncio.Semaphore(max(1, concurrency))

    async with settings.client() as client:

        async def one(asin):
            async with limit:
                return await get_product(client, asin)

        results = await asyncio.gather(
            *(one(asin) for asin in asins), return_exceptions=True
        )

    details, failures = [], []
    for asin, result in zip(asins, results):
        if isinstance(result, BaseException):
            failures.append((asin, to_amz_error(result)))
        else:
            details.append(result)
    return details, failures


def _report_total_failure(failures):
    """Exit non-zero when nothing could be fetched."""
    if len(failures) == 1:
        # A single ASIN keeps the old shape: one clean line, the error's own
        # exit code, handled at the CLI boundary.
        raise failures[0][1]

    for asin, exc in failures:
        err_console.print(f"[yellow]Warning:[/] {asin}: {exc}")
    codes = {exc.exit_code for _, exc in failures}
    error(f"All {len(failures)} product lookups failed.", codes.pop() if len(codes) == 1 else 1)


def _emit(details, single, as_json, as_plain, as_csv):
    if as_json:
        # A single ASIN still returns a bare object, not a one-element list --
        # every existing `amz product X --json | jq .price` must keep working.
        output_json(details[0].to_dict() if single else [d.to_dict() for d in details])
    elif as_plain:
        rows = [
            [
                d.asin, d.title, d.brand, d.price, d.mrp, d.discount,
                d.rating, d.review_count, d.availability,
            ]
            for d in details
        ]
        output_plain(rows, PLAIN_HEADERS)
    elif as_csv:
        rows = [
            [
                d.asin, d.title, d.brand,
                money.rupees(d.price), d.price,
                money.rupees(d.mrp), d.mrp,
                d.discount, d.discount_pct,
                d.rating, d.review_count, d.availability,
            ]
            for d in details
        ]
        output_csv(rows, CSV_HEADERS)
    else:
        for detail in details:
            print_product_detail(detail)
