"""Product variations (the "twister") from a product page.

Amazon renders selectable variations -- size, colour, style -- as a list of
swatch `<li>` elements carrying the sibling product's ASIN. This is genuinely
useful: a different size of the same shoe is frequently a few hundred rupees
cheaper, and nothing on the default page tells you that.

Prices are usually **not** inlined on the swatches (Amazon fetches them lazily),
so `Variant.price` is often 0. That is honest rather than convenient: the
command tells you to re-run `amz product` on a specific ASIN for its price,
instead of inventing a number.
"""

import re

from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.client.types import Variant

#: The swatch list items. `data-asin` is the sibling product.
_SWATCH_SELECTOR = "li[data-asin]"

#: `color_name_2` -> `color_name`; the trailing index is the slot, not the name.
_DIMENSION_FROM_ID = re.compile(r"^([a-z_]+?)_\d+(?:-announce)?$", re.IGNORECASE)

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

#: Placeholder copy Amazon puts on swatches it has not resolved yet.
_NON_LABELS = {"see available options", "", "-"}


def _dimension_of(node) -> str:
    """`color_name`, `size_name`, `style_name`, ... or '' when unnamed."""
    for child in node.css("span[id]"):
        match = _DIMENSION_FROM_ID.match(child.attributes.get("id") or "")
        if match:
            return match.group(1).lower()
    return ""


def _label_of(node) -> str:
    """Human label for a swatch: the image alt, else its title, else announce."""
    img = node.css_first("img[alt]")
    if img:
        alt = (img.attributes.get("alt") or "").strip()
        if alt and alt.lower() not in _NON_LABELS:
            return alt

    # The dedicated title node, when Amazon renders one. Preferred over the
    # `-announce` text because the announce span also swallows the slot-info
    # placeholder, yielding labels like "43 inchesSee available options".
    title = node.css_first("span.swatch-title-text, span.swatch-title-text-display")
    if title:
        text = re.sub(r"\s+", " ", title.text(strip=True)).strip()
        if text and text.lower() not in _NON_LABELS:
            return text[:80]

    announce = node.css_first("span[id$='-announce']")
    if announce:
        text = re.sub(r"\s+", " ", announce.text(strip=True)).strip()
        if text and text.lower() not in _NON_LABELS:
            return text

    text = re.sub(r"\s+", " ", node.text(strip=True)).strip()
    return "" if text.lower() in _NON_LABELS else text[:80]


def _price_of(node) -> int:
    """Inline price in paise, when Amazon bothered to render one."""
    for selector in ("span.a-price span.a-offscreen", "span.a-color-price", "span.a-price-whole"):
        found = node.css_first(selector)
        if found:
            paise = money.parse_paise(found.text(strip=True))
            if paise:
                return paise
    return 0


def parse_variants(html: str) -> list[Variant]:
    """Every selectable variation on a product page.

    Returns ``[]`` for a product with no variations -- that is the common case
    and is not an error.
    """
    if not html:
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover -- selectolax is very forgiving
        return []

    scope = tree.css_first("#twister_feature_div") or tree.css_first("#twister") or tree
    seen: set[tuple[str, str]] = set()
    variants: list[Variant] = []

    for node in scope.css(_SWATCH_SELECTOR):
        asin = (node.attributes.get("data-asin") or "").strip().upper()
        if not _ASIN_RE.match(asin):
            continue
        dimension = _dimension_of(node)
        # An ASIN legitimately repeats across dimensions -- the swatch you are
        # currently on is the same product under "Colour", "Size" and "Style".
        # De-duplicating on the ASIN alone therefore deleted the selected option
        # from every dimension after the first: a four-dimension MacBook page
        # lost 4 of its 10 swatches and showed no current selection.
        key = (dimension, asin)
        if key in seen:
            continue
        seen.add(key)

        attrs = node.attributes
        variants.append(
            Variant(
                asin=asin,
                label=_label_of(node),
                dimension=dimension,
                price=_price_of(node),
                selected=attrs.get("data-initiallyselected") == "true",
                # Amazon spells the negative, so absence means available.
                available=attrs.get("data-initiallyunavailable") != "true",
            )
        )

    return variants


def group_by_dimension(variants: list[Variant]) -> dict[str, list[Variant]]:
    """Bucket variants by dimension, preserving page order within each bucket."""
    grouped: dict[str, list[Variant]] = {}
    for variant in variants:
        grouped.setdefault(variant.dimension or "option", []).append(variant)
    return grouped
