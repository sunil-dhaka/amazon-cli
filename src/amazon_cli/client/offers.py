"""Buying options for a product.

**What this can and cannot do.** Amazon's full marketplace list -- the "Other
Sellers on Amazon" panel with every third-party price -- is loaded by an AJAX
call after the page renders. That endpoint (`/gp/product/ajax/...aodAjaxMain`)
now returns 404 to an unauthenticated client, and `/gp/offer-listing/<asin>`
simply redirects back to the product page, whose `#all-offers-display` is an
empty shell with a spinner.

So this module reports the offer that **is** server-rendered: the buy box --
price, who it ships from, who sells it, delivery and availability. That is one
offer, not a marketplace. The command says so rather than presenting a
one-row table as though it were the whole market.

If Amazon ever server-renders the full list again, `parse_offers` already looks
for the `#aod-offer` rows first and will pick them up with no other change --
and `test_offers.py` asserts the current reality, so the day it changes, a test
tells us.
"""

import re

from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.client.base import AmazonClient, validate_asin
from amazon_cli.client.types import Offer

#: Rows of the full marketplace list, when present.
_AOD_ROW_SELECTOR = "div#aod-offer, div[id^='aod-offer-'], div.olpOffer"

#: A matched node is a *row* only if its id is the row id. The prefix selector
#: above also matches `aod-offer-price` / `aod-offer-soldBy`, which are the
#: *parts* of a row -- counting those as rows duplicated every offer and gave
#: the duplicates no seller and the wrong condition.
_AOD_ROW_ID = re.compile(r"^aod-offer(?:-\d+)?$")

#: Where a price may legitimately live inside a marketplace row. Falling back to
#: the row's whole text instead would read "4.5 out of 5 stars" as Rs.4.50.
_AOD_PRICE_SELECTORS = (
    "#aod-offer-price span.a-offscreen",
    "span.a-price span.a-offscreen",
    "span.a-offscreen",
    "#aod-offer-price span.a-price-whole",
    "span.a-price span.a-price-whole",
    "span.a-color-price",
)

_LABEL_SPLIT = re.compile(r"(Ships from|Sold by|Payment|Returns)")

#: The leading number of a free-text seller rating ("4.5 out of 5", "94%").
_RATING_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _buy_box_price(tree: HTMLParser) -> int:
    """The price in the buy box, in paise. Struck-through nodes are skipped."""
    from amazon_cli.client.parser import _parse_buy_box_price

    return _parse_buy_box_price(tree)


def _labelled_fields(tree: HTMLParser) -> dict[str, str]:
    """Pull "Ships from" / "Sold by" out of the buy-box feature block.

    Amazon renders these as a label and value that collapse into one run of text
    (`'Ships fromAmazonSold byClicktech Retail Private Ltd'`), so the text is
    split on the labels rather than read positionally.
    """
    fields: dict[str, str] = {}

    for node in tree.css("div.offer-display-feature-text, div[class*='offer-display-feature']"):
        text = _clean(node.text(strip=True))
        if not text:
            continue
        parts = [p for p in _LABEL_SPLIT.split(text) if p.strip()]
        for label, value in zip(parts, parts[1:]):
            key = label.lower()
            if key in ("ships from", "sold by") and key not in fields:
                # The value often repeats the name twice; keep one copy.
                value = _clean(value)
                half = len(value) // 2
                if half and value[:half] == value[half:]:
                    value = value[:half]
                fields[key] = value[:80]

    if "sold by" not in fields:
        seller = tree.css_first("#sellerProfileTriggerId")
        if seller:
            fields["sold by"] = _clean(seller.text(strip=True))[:80]
    return fields


def _availability(tree: HTMLParser) -> str:
    node = tree.css_first("div#availability") or tree.css_first("#availability")
    if not node:
        return ""
    span = node.css_first("span") or node
    return re.sub(r"\{.*", "", _clean(span.text(strip=True))).strip()[:80]


def _delivery(tree: HTMLParser) -> str:
    for selector in (
        "div#mir-layout-DELIVERY_BLOCK",
        "div#deliveryBlockMessage",
        "span[data-csa-c-delivery-time]",
    ):
        node = tree.css_first(selector)
        if node:
            return _clean(node.text(strip=True))[:100]
    return ""


def _aod_row_price(row) -> int:
    """The item price of a marketplace row, in paise, or 0 when it has none.

    Only price-shaped nodes are considered. A row with no price at all (a
    "currently unavailable" listing, say) must yield nothing rather than the
    first number in its prose.
    """
    for selector in _AOD_PRICE_SELECTORS:
        node = row.css_first(selector)
        if node:
            price = money.parse_paise(_clean(node.text(strip=True)))
            if price:
                return price
    return 0


def _aod_rows(tree: HTMLParser) -> list:
    """The marketplace row containers, excluding their own sub-sections."""
    rows = []
    for node in tree.css(_AOD_ROW_SELECTOR):
        node_id = (node.attributes.get("id") or "").strip()
        classes = (node.attributes.get("class") or "").split()
        if _AOD_ROW_ID.match(node_id) or "olpOffer" in classes:
            rows.append(node)
    return rows


def _parse_aod_rows(tree: HTMLParser) -> list[Offer]:
    """The full marketplace list, if Amazon ever renders it server-side."""
    offers: list[Offer] = []
    for row in _aod_rows(tree):
        price = _aod_row_price(row)
        if not price:
            continue
        seller_node = row.css_first("#aod-offer-soldBy a, a[href*='seller=']")
        shipping_node = row.css_first("span[class*='delivery'], #mir-layout-DELIVERY_BLOCK")
        condition_node = row.css_first("#aod-offer-heading, h5")
        offers.append(
            Offer(
                price=price,
                seller=_clean(seller_node.text(strip=True))[:80] if seller_node else "",
                condition=_clean(condition_node.text(strip=True))[:40] if condition_node else "New",
                delivery=_clean(shipping_node.text(strip=True))[:100] if shipping_node else "",
                is_prime=bool(row.css_first("i.a-icon-prime")),
            )
        )
    return offers


def parse_offers(html: str) -> list[Offer]:
    """Every buying option this page actually carries.

    Returns the marketplace rows when present, otherwise a single-element list
    holding the buy-box offer, otherwise ``[]``.
    """
    if not html:
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover
        return []

    rows = _parse_aod_rows(tree)
    if rows:
        return rows

    price = _buy_box_price(tree)
    if not price:
        return []

    fields = _labelled_fields(tree)
    return [
        Offer(
            price=price,
            shipping=0,
            condition="New",
            seller=fields.get("sold by", ""),
            ships_from=fields.get("ships from", ""),
            delivery=_delivery(tree),
            is_prime=bool(tree.css_first("i.a-icon-prime")),
        )
    ]


def _rating_value(text: str) -> float:
    """The number in a seller rating string, or -1 when there is none.

    ``seller_rating`` is free text ("4.5 out of 5", "94% positive"), so ordering
    on the raw string is lexicographic -- which ranks "95% positive" above
    "100% positive" and "9.4" above "10". Only the leading number is compared.
    """
    match = _RATING_NUMBER.search(text or "")
    return float(match.group(1)) if match else -1.0


def sort_offers(offers: list[Offer], key: str = "total") -> list[Offer]:
    """Order offers so the one you should actually buy is first.

    Default is **total** (price + shipping): a cheaper item with a delivery
    charge is routinely the worse deal, and sorting on price alone hides that.
    """
    if key == "price":
        return sorted(offers, key=lambda o: (o.price <= 0, o.price))
    if key == "rating":
        # Best rated first; unrated sellers last, then cheapest total breaks ties.
        return sorted(
            offers,
            key=lambda o: (-_rating_value(o.seller_rating), o.total <= 0, o.total),
        )
    return sorted(offers, key=lambda o: (o.total <= 0, o.total))


async def get_offers(client: AmazonClient, asin: str) -> list[Offer]:
    """Fetch and parse the buying options for an ASIN."""
    asin = validate_asin(asin)
    html = await client.fetch(f"/gp/offer-listing/{asin}")
    offers = parse_offers(html)
    if offers:
        return offers
    # The offer-listing URL redirects to the product page for most ASINs; if it
    # yielded nothing at all, try the canonical page before giving up.
    return parse_offers(await client.fetch(f"/dp/{asin}"))
