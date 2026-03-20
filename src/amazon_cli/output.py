"""Output formatters: rich tables, JSON, and plain text."""

import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
err_console = Console(stderr=True)


def output_json(data) -> None:
    """Print structured JSON to stdout."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def output_plain(rows: list[list[str]], headers: list[str] | None = None) -> None:
    """Print tab-separated rows with no ANSI (pipeable)."""
    if headers:
        print("\t".join(headers))
    for row in rows:
        print("\t".join(str(cell) for cell in row))


def error(msg: str) -> None:
    """Print error to stderr and exit."""
    err_console.print(f"[bold red]Error:[/] {msg}")
    sys.exit(1)


def _format_price(price: int, mrp: int = 0) -> Text:
    """Format price with optional strikethrough MRP."""
    text = Text()
    if not price:
        text.append("-", style="dim")
        return text
    text.append(f"Rs.{price:,}", style="bold green")
    if mrp and mrp > price:
        text.append(f" Rs.{mrp:,}", style="dim strike")
        pct = round((1 - price / mrp) * 100)
        text.append(f" ({pct}% off)", style="bold red")
    return text


def _format_rating(rating: float, count: int = 0) -> str:
    """Format rating as stars string with optional count."""
    full = int(rating)
    half = 1 if rating - full >= 0.5 else 0
    empty = 5 - full - half
    stars = "*" * full + ("+" if half else "") + "." * empty
    base = f"{rating:.1f} [{stars}]"
    if count:
        base += f" ({count:,})"
    return base


def print_products_table(products, total_count: int = 0, page: int = 1) -> None:
    """Render a search results table."""
    title = f"Search Results (page {page}"
    if total_count:
        title += f", ~{total_count} found"
    title += ")"

    table = Table(title=title, show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("ASIN", style="cyan", width=12)
    table.add_column("Title", max_width=45)
    table.add_column("Price", justify="right")
    table.add_column("Rating", justify="center", width=12)
    table.add_column("Reviews", justify="right", width=8)

    for i, p in enumerate(products, 1):
        rating = _format_rating(p.rating) if p.rating else "-"
        reviews = f"{p.review_count:,}" if p.review_count else "-"
        title_text = p.title[:45] + "..." if len(p.title) > 45 else p.title
        price = _format_price(p.price)

        table.add_row(str(i), p.asin, title_text, price, rating, reviews)

    console.print(table)


def print_product_detail(product) -> None:
    """Render full product details as a rich panel."""
    lines = []

    if product.brand:
        lines.append(f"[bold cyan]{product.brand}[/] [bold]{product.title}[/]")
    else:
        lines.append(f"[bold]{product.title}[/]")
    lines.append("")

    price_str = str(_format_price(product.price, product.mrp))
    if product.discount and not product.mrp:
        price_str += f"  [bold red]{product.discount}[/]"
    lines.append(f"Price: {price_str}")

    if product.rating:
        lines.append(f"Rating: [yellow]{_format_rating(product.rating, product.review_count)}[/]")

    if product.availability:
        style = "green" if "In stock" in product.availability else "red"
        lines.append(f"Stock: [{style}]{product.availability}[/]")

    if product.features:
        lines.append("")
        lines.append("[bold]About this item:[/]")
        for feat in product.features[:8]:
            lines.append(f"  - {feat[:120]}")

    if product.specs:
        lines.append("")
        lines.append("[bold]Specifications:[/]")
        for key, val in list(product.specs.items())[:12]:
            lines.append(f"  [dim]{key}:[/] {val}")

    panel = Panel("\n".join(lines), title=f"[cyan]{product.asin}[/]", border_style="blue")
    console.print(panel)

    # Review insights (rendered after the panel)
    if product.insights and (product.insights.summary or product.insights.aspects):
        _print_review_insights(product.insights)


def print_reviews(reviews, asin: str, page: int = 1) -> None:
    """Render product reviews."""
    console.print(f"\n[bold]Reviews for {asin}[/] (page {page})\n")

    for r in reviews:
        stars = _format_rating(r.rating)
        verified = " [green](Verified)[/]" if r.verified else ""
        header = f"[yellow]{stars}[/]{verified}  [bold]{r.title}[/]"
        meta = f"[dim]{r.author} -- {r.date}[/]"

        console.print(header)
        console.print(meta)
        if r.body:
            console.print(f"  {r.body[:300]}")
        console.print()


def print_compare_table(products) -> None:
    """Side-by-side comparison table."""
    table = Table(title="Product Comparison", show_lines=True)
    table.add_column("Attribute", style="bold", width=15)

    for p in products:
        table.add_column(p.asin, max_width=30)

    rows = [
        ("Title", [p.title[:30] for p in products]),
        ("Brand", [p.brand or "-" for p in products]),
        ("Price", [p.price_display or "-" for p in products]),
        ("MRP", [p.mrp_display or "-" for p in products]),
        ("Discount", [p.discount or f"-{p.discount_pct}%" if p.discount_pct else "-" for p in products]),
        ("Rating", [_format_rating(p.rating) if p.rating else "-" for p in products]),
        ("Reviews", [f"{p.review_count:,}" if p.review_count else "-" for p in products]),
        ("Stock", [p.availability or "-" for p in products]),
    ]

    for label, values in rows:
        table.add_row(label, *values)

    console.print(table)


def _print_review_insights(insights) -> None:
    """Render review insights: AI summary, aspects, and histogram."""
    lines = []

    if insights.summary:
        lines.append(f"[dim italic]{insights.summary}[/]")
        lines.append("")

    if insights.histogram:
        lines.append("[bold]Rating breakdown:[/]")
        for stars in sorted(insights.histogram.keys(), reverse=True):
            pct = insights.histogram[stars]
            bar = "#" * (pct // 2)
            lines.append(f"  {stars} star  [yellow]{bar:<25}[/] {pct}%")
        lines.append("")

    if insights.aspects:
        lines.append("[bold]What customers say:[/]")
        for a in insights.aspects:
            pos_pct = round(a.positive / a.total * 100) if a.total else 0
            if pos_pct >= 80:
                style = "green"
            elif pos_pct >= 60:
                style = "yellow"
            else:
                style = "red"
            lines.append(
                f"  [{style}]{a.name:<20}[/] {a.total:>3} mentions  "
                f"[green]+{a.positive}[/] [red]-{a.negative}[/]"
            )

    if lines:
        panel = Panel("\n".join(lines), title="[yellow]Customer Insights[/]", border_style="yellow")
        console.print(panel)
