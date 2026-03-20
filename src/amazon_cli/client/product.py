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
    """Fetch and parse product reviews from the product page.

    Amazon.in gates /product-reviews/ behind login, so we extract
    the ~13 top reviews embedded in the product page SSR HTML.
    Pagination is not available without authentication.
    """
    html = await client.fetch(f"/dp/{asin}")
    return parse_reviews_page(html)
