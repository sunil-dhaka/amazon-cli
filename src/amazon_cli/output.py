"""Output formatters: rich, JSON, plain."""

import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
err_console = Console(stderr=True)


def error(msg: str) -> None:
    """Print error and exit."""
    err_console.print(f"[bold red]Error:[/] {msg}")
    sys.exit(1)


def output_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def output_plain(rows: list[list[str]], headers: list[str] | None = None) -> None:
    """Tab-separated rows (pipeable)."""
    if headers:
        print("\t".join(headers))
    for row in rows:
        print("\t".join(str(cell) for cell in row))


def print_products_table(products, total_count: int = 0, page: int = 1) -> None:
    """Rich table of search results."""
    title = f"Search Results (page {page}"
    if total_count:
        title += f", ~{total_count} found"
    title += ")"

    table = Table(title=title, show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("ASIN", style="cyan", width=12)
    table.add_column("Title", max_width=50)
    table.add_column("Price", style="green", justify="right")
    table.add_column("Rating", justify="center")
    table.add_column("Reviews", justify="right")
    table.add_column("Prime", justify="center")

    for i, p in enumerate(products, 1):
        rating_text = _format_rating(p.rating) if p.rating else "-"
        reviews = f"{p.review_count:,}" if p.review_count else "-"
        prime = "Yes" if p.is_prime else ""
        price = p.price if p.price else "-"
        title_text = p.title[:50] + "..." if len(p.title) > 50 else p.title

        table.add_row(str(i), p.asin, title_text, price, rating_text, reviews, prime)

    console.print(table)


def print_product_detail(product) -> None:
    """Rich panel for a single product."""
    lines = []

    if product.brand:
        lines.append(f"[dim]Brand:[/] {product.brand}")
    lines.append(f"[bold]{product.title}[/]")
    lines.append("")

    if product.price:
        price_line = f"[bold green]Price: {product.price}[/]"
        if product.mrp and product.mrp != product.price:
            price_line += f"  [dim strikethrough]{product.mrp}[/]"
        if product.discount:
            price_line += f"  [bold red]{product.discount}[/]"
        lines.append(price_line)

    if product.rating:
        lines.append(f"Rating: {_format_rating(product.rating)}  ({product.review_count:,} reviews)")

    if product.availability:
        lines.append(f"Stock: {product.availability}")

    if product.features:
        lines.append("")
        lines.append("[bold]Features:[/]")
        for feat in product.features:
            lines.append(f"  - {feat}")

    if product.specs:
        lines.append("")
        lines.append("[bold]Specifications:[/]")
        for key, val in product.specs.items():
            lines.append(f"  {key}: {val}")

    panel = Panel("\n".join(lines), title=f"[cyan]{product.asin}[/]", border_style="blue")
    console.print(panel)


def print_reviews(reviews, asin: str, page: int = 1) -> None:
    """Rich output for reviews."""
    console.print(f"\n[bold]Reviews for {asin}[/] (page {page})\n")

    for i, r in enumerate(reviews, 1):
        stars = _format_rating(r.rating)
        verified = " [green](Verified)[/]" if r.verified else ""
        header = f"{stars}{verified}  [bold]{r.title}[/]"
        meta = f"[dim]{r.author} -- {r.date}[/]"

        console.print(header)
        console.print(meta)
        if r.body:
            console.print(r.body)
        console.print()


def print_compare_table(products) -> None:
    """Side-by-side comparison table."""
    table = Table(title="Product Comparison", show_lines=True)
    table.add_column("Attribute", style="bold", width=15)

    for p in products:
        label = p.asin
        table.add_column(label, max_width=35)

    rows = [
        ("Title", [p.title[:35] for p in products]),
        ("Brand", [p.brand or "-" for p in products]),
        ("Price", [p.price or "-" for p in products]),
        ("MRP", [p.mrp or "-" for p in products]),
        ("Discount", [p.discount or "-" for p in products]),
        ("Rating", [_format_rating(p.rating) if p.rating else "-" for p in products]),
        ("Reviews", [f"{p.review_count:,}" if p.review_count else "-" for p in products]),
        ("In Stock", [p.availability or "-" for p in products]),
    ]

    for label, values in rows:
        table.add_row(label, *values)

    console.print(table)


def _format_rating(rating: float) -> str:
    """Format rating as stars string."""
    full = int(rating)
    half = 1 if rating - full >= 0.5 else 0
    empty = 5 - full - half
    stars = "*" * full + ("+" if half else "") + "." * empty
    return f"{rating:.1f} [{stars}]"
