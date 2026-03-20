"""Data types for Amazon product data."""

from dataclasses import dataclass, field


@dataclass
class Product:
    """Product from search results."""

    asin: str
    title: str
    rating: float = 0.0
    review_count: int = 0
    price: str = ""
    image_url: str = ""
    is_prime: bool = False
    delivery: str = ""

    @classmethod
    def from_search(cls, raw: dict) -> "Product":
        """Parse from search result attributes JSON."""
        return cls(
            asin=raw.get("asin", ""),
            title=raw.get("title", ""),
            rating=float(raw.get("rating", 0) or 0),
            review_count=int(raw.get("reviewCount", 0) or 0),
            image_url=raw.get("imageUrl", ""),
            is_prime=bool(raw.get("isPrime", False)),
        )

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
class ProductDetail:
    """Full product details from a product page."""

    asin: str
    title: str = ""
    brand: str = ""
    price: str = ""
    mrp: str = ""
    discount: str = ""
    rating: float = 0.0
    review_count: int = 0
    availability: str = ""
    features: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    image_url: str = ""

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
