"""amz variants command."""

import asyncio

import click
from rich.table import Table

from amazon_cli import money
from amazon_cli.client.base import validate_asin
from amazon_cli.client.variants import group_by_dimension, parse_variants
from amazon_cli.context import AmzContext, to_amz_error
from amazon_cli.output import console, output_csv, output_json, output_plain

PLAIN_HEADERS = ["dimension", "asin", "label", "price", "selected", "available"]

CSV_HEADERS = ["dimension", "asin", "label", "price", "price_paise", "selected", "available"]

#: Human names for Amazon's internal dimension keys.
DIMENSION_LABELS = {
    "color_name": "Colour",
    "size_name": "Size",
    "style_name": "Style",
    "pattern_name": "Pattern",
    "flavor_name": "Flavour",
    "material_name": "Material",
    "capacity_name": "Capacity",
}


@click.command()
@click.argument("asin")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--plain", "as_plain", is_flag=True, help="Output as plain TSV.")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.pass_context
def variants(ctx, asin, as_json, as_plain, as_csv):
    """List the size/colour/style variations of ASIN.

    Useful because a different size of the same product is often cheaper, and
    nothing on the product page tells you so. Amazon rarely inlines the sibling
    prices, so run `amz product <ASIN>` on one to see what it costs.
    """
    settings = AmzContext.current(ctx)
    # Validated here so a malformed ASIN exits 2 (InputError) instead of
    # escaping as a bare ValueError traceback with exit 1.
    try:
        asin = validate_asin(asin)
    except ValueError as exc:
        raise to_amz_error(exc) from exc

    found = asyncio.run(_fetch(settings, asin))

    if as_json:
        output_json([v.to_dict() for v in found])
        return
    if as_plain:
        output_plain(
            [[v.dimension, v.asin, v.label, v.price, v.selected, v.available] for v in found],
            PLAIN_HEADERS,
        )
        return
    if as_csv:
        output_csv(
            [[v.dimension, v.asin, v.label, money.rupees(v.price), v.price,
              v.selected, v.available] for v in found],
            CSV_HEADERS,
        )
        return

    if not found:
        console.print(
            f"[yellow]{asin.upper()} has no selectable variations.[/] "
            "Most products are sold as a single item."
        )
        return
    _render(found, asin)


async def _fetch(settings, asin):
    asin = validate_asin(asin)
    async with settings.client() as client:
        return parse_variants(await client.fetch(f"/dp/{asin}"))


def _render(found, asin) -> None:
    grouped = group_by_dimension(found)
    console.print(
        f"\n[bold]{asin.upper()}[/] has {len(found)} variation"
        f"{'s' if len(found) != 1 else ''} across {len(grouped)} "
        f"dimension{'s' if len(grouped) != 1 else ''}\n"
    )

    for dimension, items in grouped.items():
        label = DIMENSION_LABELS.get(dimension, dimension.replace("_", " ").title())
        table = Table(title=f"{label} ({len(items)})", show_lines=False, title_justify="left")
        table.add_column(" ", width=2)
        table.add_column("ASIN", style="cyan", width=12)
        table.add_column("Option", max_width=44)
        table.add_column("Price", justify="right")

        for item in items:
            marker = "*" if item.selected else ""
            price = money.format_inr(item.price) if item.price else "[dim]-[/]"
            row_style = "bold" if item.selected else ("dim" if not item.available else None)
            table.add_row(marker, item.asin, item.label or "-", price, style=row_style)
        console.print(table)

    console.print("[dim]* current selection.  Prices are shown only where Amazon inlines them; "
                  "run `amz product <ASIN>` for a specific one.[/]")
