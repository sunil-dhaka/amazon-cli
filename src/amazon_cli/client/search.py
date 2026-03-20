"""Amazon.in search via SSR HTML."""

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.parser import parse_search_results
from amazon_cli.client.types import Product

SORT_OPTIONS = {
    "relevance": None,
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "reviews": "review-rank",
    "newest": "date-desc-rank",
}


def _build_search_path(query: str, page: int = 1, sort: str | None = None) -> str:
    """Build Amazon search URL path."""
    params = [f"k={query}"]
    if page > 1:
        params.append(f"page={page}")
    if sort and sort in SORT_OPTIONS and SORT_OPTIONS[sort]:
        params.append(f"s={SORT_OPTIONS[sort]}")
    return "/s?" + "&".join(params)


async def search_products(
    client: AmazonClient,
    query: str,
    page: int = 1,
    sort: str | None = None,
) -> tuple[list[Product], int]:
    """Search Amazon.in and return (products, total_count)."""
    path = _build_search_path(query, page, sort)
    html = await client.fetch(path)
    return parse_search_results(html)
