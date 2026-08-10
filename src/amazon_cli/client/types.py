"""Data types for Amazon product data.

All money fields are **paise** (``int``). See :mod:`amazon_cli.money`.
"""

import re
from dataclasses import dataclass, field

from amazon_cli import money

# Re-exported so existing imports keep working; the implementations now live in
# `amazon_cli.money`, which is tested independently.
_parse_price = money.parse_paise
_format_price = money.format_inr


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip stray HTML/JS artifacts."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


@dataclass
class Product:
    """Product from search results."""

    asin: str
    title: str
    rating: float = 0.0
    review_count: int = 0
    price: int = 0
    """Price in paise."""
    image_url: str = ""
    is_prime: bool = False
    delivery: str = ""

    @property
    def price_display(self) -> str:
        return money.format_inr(self.price)

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "rating": self.rating,
            "review_count": self.review_count,
            "price": money.rupees(self.price),
            "price_paise": self.price,
            "image_url": self.image_url,
            "is_prime": self.is_prime,
            "delivery": self.delivery,
        }


@dataclass
class ReviewAspect:
    """A review aspect tag (e.g. Quality: 67 mentions, 59 positive)."""

    name: str
    total: int
    positive: int
    negative: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total": self.total,
            "positive": self.positive,
            "negative": self.negative,
        }


@dataclass
class ReviewInsights:
    """AI-generated review summary and aspect breakdown."""

    summary: str = ""
    aspects: list[ReviewAspect] = field(default_factory=list)
    histogram: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "aspects": [a.to_dict() for a in self.aspects],
            "histogram": self.histogram,
        }


@dataclass
class Offer:
    """One seller's offer from the offer-listing page."""

    price: int = 0
    """Price in paise."""
    shipping: int = 0
    """Delivery charge in paise; 0 means free or unstated."""
    condition: str = ""
    seller: str = ""
    seller_rating: str = ""
    delivery: str = ""
    is_prime: bool = False
    ships_from: str = ""

    @property
    def total(self) -> int:
        """Price plus shipping -- the number that actually decides the winner."""
        return self.price + self.shipping if self.price else 0

    @property
    def price_display(self) -> str:
        return money.format_inr(self.price)

    def to_dict(self) -> dict:
        return {
            "price": money.rupees(self.price),
            "price_paise": self.price,
            "shipping": money.rupees(self.shipping),
            "shipping_paise": self.shipping,
            "total": money.rupees(self.total),
            "total_paise": self.total,
            "condition": self.condition,
            "seller": self.seller,
            "seller_rating": self.seller_rating,
            "delivery": self.delivery,
            "is_prime": self.is_prime,
            "ships_from": self.ships_from,
        }


@dataclass
class Variant:
    """One selectable variation (size, colour, style) of a product."""

    asin: str
    label: str
    dimension: str = ""
    price: int = 0
    """Price in paise; 0 when Amazon does not inline it."""
    selected: bool = False
    available: bool = True

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "label": self.label,
            "dimension": self.dimension,
            "price": money.rupees(self.price),
            "price_paise": self.price,
            "selected": self.selected,
            "available": self.available,
        }


@dataclass
class Deal:
    """One entry from the deals or bestsellers listing."""

    asin: str
    title: str
    price: int = 0
    """Price in paise."""
    mrp: int = 0
    discount: str = ""
    rank: int = 0
    rating: float = 0.0
    review_count: int = 0
    image_url: str = ""
    badge: str = ""

    @property
    def discount_percent(self) -> int:
        return money.discount_percent(self.price, self.mrp)

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "price": money.rupees(self.price),
            "price_paise": self.price,
            "mrp": money.rupees(self.mrp),
            "mrp_paise": self.mrp,
            "discount": self.discount,
            "discount_percent": self.discount_percent,
            "rank": self.rank,
            "rating": self.rating,
            "review_count": self.review_count,
            "image_url": self.image_url,
            "badge": self.badge,
        }


@dataclass
class ProductDetail:
    """Full product details from a product page."""

    asin: str
    title: str = ""
    brand: str = ""
    price: int = 0
    """Price in paise."""
    mrp: int = 0
    """Struck-through list price in paise."""
    discount: str = ""
    rating: float = 0.0
    review_count: int = 0
    availability: str = ""
    features: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    image_url: str = ""
    insights: ReviewInsights = field(default_factory=ReviewInsights)
    variants: list[Variant] = field(default_factory=list)

    @property
    def price_display(self) -> str:
        return money.format_inr(self.price)

    @property
    def mrp_display(self) -> str:
        return money.format_inr(self.mrp)

    @property
    def discount_pct(self) -> int:
        return money.discount_percent(self.price, self.mrp)

    @property
    def in_stock(self) -> bool:
        text = self.availability.lower()
        return bool(self.price) and "unavailable" not in text and "out of stock" not in text

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "brand": self.brand,
            "price": money.rupees(self.price),
            "price_paise": self.price,
            "mrp": money.rupees(self.mrp),
            "mrp_paise": self.mrp,
            "discount": self.discount,
            "discount_percent": self.discount_pct,
            "rating": self.rating,
            "review_count": self.review_count,
            "availability": self.availability,
            "in_stock": self.in_stock,
            "features": self.features,
            "specs": self.specs,
            "image_url": self.image_url,
            "insights": self.insights.to_dict(),
            "variants": [v.to_dict() for v in self.variants],
        }


@dataclass
class Review:
    """A single product review."""

    title: str = ""
    body: str = ""
    rating: float = 0.0
    author: str = ""
    date: str = ""
    verified: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "rating": self.rating,
            "author": self.author,
            "date": self.date,
            "verified": self.verified,
        }
