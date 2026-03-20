"""Data types for Amazon product data."""

import re
from dataclasses import dataclass, field


def _parse_price(text: str) -> int:
    """Parse a price string like '₹3,325.00' into paise-free int (3325)."""
    if not text:
        return 0
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return 0
    return int(float(cleaned))


def _format_price(price: int) -> str:
    """Format an int price to display string."""
    if not price:
        return ""
    return f"Rs.{price:,}"


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip stray HTML/JS artifacts."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Product:
    """Product from search results."""

    asin: str
    title: str
    rating: float = 0.0
    review_count: int = 0
    price: int = 0
    image_url: str = ""
    is_prime: bool = False
    delivery: str = ""

    @property
    def price_display(self) -> str:
        return _format_price(self.price)

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "rating": self.rating,
            "review_count": self.review_count,
            "price": self.price,
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
class ProductDetail:
    """Full product details from a product page."""

    asin: str
    title: str = ""
    brand: str = ""
    price: int = 0
    mrp: int = 0
    discount: str = ""
    rating: float = 0.0
    review_count: int = 0
    availability: str = ""
    features: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    image_url: str = ""
    insights: ReviewInsights = field(default_factory=ReviewInsights)

    @property
    def price_display(self) -> str:
        return _format_price(self.price)

    @property
    def mrp_display(self) -> str:
        return _format_price(self.mrp)

    @property
    def discount_pct(self) -> int:
        if self.price and self.mrp and self.price < self.mrp:
            return round((1 - self.price / self.mrp) * 100)
        return 0

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "brand": self.brand,
            "price": self.price,
            "mrp": self.mrp,
            "discount": self.discount,
            "rating": self.rating,
            "review_count": self.review_count,
            "availability": self.availability,
            "features": self.features,
            "specs": self.specs,
            "image_url": self.image_url,
            "insights": self.insights.to_dict(),
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
