"""Amazon.in product detail and reviews via SSR HTML."""

from amazon_cli.client.base import AmazonClient
from amazon_cli.client.parser import parse_product_page, parse_reviews_page
from amazon_cli.client.types import ProductDetail, Review


async def get_product(client: AmazonClient, asin: str) -> ProductDetail:
    """Fetch and parse a product detail page."""
    html = await client.fetch(f"/dp/{asin}")
    return parse_product_page(html, asin)


async def get_reviews(
    client: AmazonClient,
    asin: str,
    page: int = 1,
) -> list[Review]:
    """Fetch and parse product reviews.

    Tries dedicated reviews page first, falls back to product page
    (which embeds top reviews in the SSR HTML).
    """
    if page == 1:
        # Product page has ~10 reviews embedded, try that first
        html = await client.fetch(f"/dp/{asin}")
        reviews = parse_reviews_page(html)
        if reviews:
            return reviews
    # Try dedicated reviews page
    params = f"?pageNumber={page}" if page > 1 else ""
    html = await client.fetch(f"/product-reviews/{asin}{params}")
    return parse_reviews_page(html)
