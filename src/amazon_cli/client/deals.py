"""Amazon.in deals and bestsellers listings.

Two very different pages feed the same :class:`~amazon_cli.client.types.Deal`
contract:

``/deals``
    A "deals card layout" (``dcl-``) surface. Most of the page is client
    rendered -- the hero banners, the filter rail and the infinite-scroll tail
    all arrive as JSON over XHR -- but the initial carousels ("Best Deals",
    "Featured most-loved picks") *are* server rendered as ``div.dcl-product``
    cards, and those carry everything we need: ASIN, title, sale price, struck
    M.R.P. and the discount badge. There is no rating on a deal card, so
    ``rating``/``review_count`` stay zero, and there is no rank, so ``rank``
    records the card's position in Amazon's own page order.

``/gp/bestsellers[/<slug>]``
    A conventional server-rendered p13n grid. A single department renders 30
    ranked cards in ``div#gridItemRoot``; the "all departments" landing page
    instead renders six carousels of six, so ranks there run 1..6 *per
    department* rather than 1..N overall. Both shapes hang the item off a
    ``div[data-asin]`` wrapper containing a ``span.zg-bdg-text`` rank badge,
    which is what this module keys on.

Every selector here is chosen to survive Amazon's hashed CSS class names
(``_cDEzb_p13n-sc-price_3mJ9Z`` and friends), which are rebuilt on every
deploy. Where a hashed class is the only handle, we match on a stable
substring (``[class*='line-clamp']``) or fall back to structure.
"""

import difflib
import re

from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.client.base import AmazonClient, looks_like_bot_check
from amazon_cli.client.parser import _STRUCK_ANCESTORS, _parse_count
from amazon_cli.client.types import Deal, _clean_text
from amazon_cli.errors import BotCheckError, InputError

DEALS_PATH = "/deals"
BESTSELLERS_PATH = "/gp/bestsellers"

#: Top-level bestseller departments, slug -> display label.
#:
#: Read straight out of the captured ``/gp/bestsellers`` nav tree rather than
#: guessed, which is why the odd ones are right: Jewellery lives at ``jewelry``,
#: Home & Kitchen at ``kitchen``, Clothing at ``apparel``.
BESTSELLER_CATEGORIES: dict[str, str] = {
    "amazon-renewed": "Amazon Renewed",
    "apparel": "Clothing & Accessories",
    "automotive": "Car & Motorbike",
    "baby": "Baby Products",
    "beauty": "Beauty",
    "books": "Books",
    "boost": "Amazon Launchpad",
    "computers": "Computers & Accessories",
    "digital-text": "Kindle Store",
    "dvd": "Movies & TV Shows",
    "electronics": "Electronics",
    "garden": "Garden & Outdoors",
    "gift-cards": "Gift Cards",
    "grocery": "Grocery & Gourmet Foods",
    "home-improvement": "Home Improvement",
    "hpc": "Health & Personal Care",
    "industrial": "Industrial & Scientific",
    "jewelry": "Jewellery",
    "kitchen": "Home & Kitchen",
    "luggage": "Bags, Wallets and Luggage",
    "mobile-apps": "Apps & Games",
    "music": "Music",
    "musical-instruments": "Musical Instruments",
    "office": "Office Products",
    "pet-supplies": "Pet Supplies",
    "shoes": "Shoes & Handbags",
    "software": "Software",
    "sports": "Sports, Fitness & Outdoors",
    "toys": "Toys & Games",
    "videogames": "Video Games",
    "watches": "Watches",
}

#: Aliases for slugs a user is likely to type from the department's own name.
_CATEGORY_ALIASES = {
    "clothing": "apparel",
    "fashion": "apparel",
    "health": "hpc",
    "home": "kitchen",
    "jewellery": "jewelry",
    "mobiles": "electronics",
    "video-games": "videogames",
}

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# ASIN as it appears in a listing href, in preference order: the canonical
# /dp/ segment first, then the tracking parameter Amazon appends, then the
# ref_ tag the deals page uses.
_ASIN_IN_HREF = (
    re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})"),
    re.compile(r"[?&]pd_rd_i=([A-Z0-9]{10})"),
    re.compile(r"ref_?=[A-Za-z_]*_([A-Z0-9]{10})"),
)

# `ref=zg_bs_g_electronics_d_sccl_18` -- department slug and rank, carried on
# every bestsellers link. A second, independent source for both fields, which
# is what lets a missing rank badge degrade to a right answer instead of a zero.
_ZG_REF = re.compile(r"zg_bs_(?:[a-z]_)?([a-z0-9-]+)_d_sccl_(\d+)")

_RANK_BADGE = re.compile(r"#\s*([\d,]+)")
_RATING_RE = re.compile(r"([\d.]+)\s+out\s+of\s+5")
_RATINGS_COUNT_RE = re.compile(r"out\s+of\s+5\s+stars,\s*([\d,]+)")
_PERCENT_RE = re.compile(r"(\d+)\s*%")

# Anything that is only a currency symbol, digits and separators is a price,
# not a title. Guards the "first link inside the card" title fallback.
_PRICE_ONLY = re.compile(r"^[\s₹ Rs.,\d]+$")

# Where a card's title ends and its badge/price furniture begins.
#
# A deals card wraps the *whole* card in a single <a>, so that link's text is
# "<title>89% offFreedom Sale Mega Deal₹22.00₹2200M.R.P:₹199.00". Taking it
# whole would print price furniture as the product name, so the fallback keeps
# only what precedes the first of these markers.
_TITLE_FURNITURE = re.compile(r"\d+\s*%\s*off|₹|M\.R\.P")


def _reject_bot_check(html: str) -> None:
    """Raise before parsing when the body is a bot-check interstitial.

    ``AmazonClient.fetch`` already does this, but a challenge page parses to an
    empty list perfectly happily, so anything that parses cached or piped HTML
    would otherwise report "no deals today" for what is really a block.
    """
    if looks_like_bot_check(html):
        raise BotCheckError()


#: Class fragments that mark a price node -- or a wrapper close above it -- as
#: the struck-through list price.
#:
#: Wider than :data:`~amazon_cli.client.parser._STRUCK_ANCESTORS` on purpose. A
#: deal card states "this is the M.R.P." four independent ways
#: (``data-a-strike``, ``a-text-price``, ``dcl-product-price-old`` on the span,
#: ``dcl-product-old-price-section`` on its wrapper) and any one of them going
#: missing must not be enough to let Rs.199 be reported as the sale price of a
#: Rs.22 hairband.
_STRUCK_CLASS_MARKERS = (*_STRUCK_ANCESTORS, "price-old", "old-price", "strike")


def _is_struck_price(node) -> bool:
    """True when this ``a-price`` block is a struck-through list price.

    :func:`amazon_cli.client.parser._is_struck` only walks *ancestors*, which is
    right for the buy box where the marker sits on a wrapper. On a deal card the
    marker is on the price span itself (``a-text-price data-a-strike="true"``),
    so the node itself is checked too -- same bounded walk, one step longer.
    """
    current = node
    for _ in range(7):  # the node itself, then the six ancestors above it
        if current is None:
            return False
        attrs = current.attributes
        if attrs.get("data-a-strike") == "true":
            return True
        classes = attrs.get("class") or ""
        if any(marker in classes for marker in _STRUCK_CLASS_MARKERS):
            return True
        current = current.parent
    return False


def _price_from_block(node) -> int:
    """Paise from an ``span.a-price`` block.

    Prefers the accessible ``a-offscreen`` copy, which is a whole formatted
    price. When a layout blanks it, rebuild the number from the visible
    ``a-price-whole`` / ``a-price-fraction`` pair rather than reading the whole
    block's text -- the block's text concatenates both copies
    (``"₹22.00₹2200"``) and is a mis-parse waiting to happen.
    """
    if node is None:
        return money.UNKNOWN

    offscreen = node.css_first("span.a-offscreen")
    if offscreen is not None:
        price = money.parse_paise(offscreen.text(strip=True))
        if price:
            return price

    whole = node.css_first("span.a-price-whole")
    if whole is None:
        return money.UNKNOWN
    text = whole.text(strip=True).rstrip(".,  ")
    fraction = node.css_first("span.a-price-fraction")
    if fraction is not None:
        digits = fraction.text(strip=True)
        if digits.isdigit():
            text = f"{text}.{digits}"
    return money.parse_paise(text)


def _asin_from_href(href: str) -> str:
    for pattern in _ASIN_IN_HREF:
        match = pattern.search(href or "")
        if match:
            return match.group(1)
    return ""


def _first_image(node) -> str:
    for selector in ("img.dcl-dynamic-image", "img.p13n-product-image", "img[src]"):
        img = node.css_first(selector)
        if img is not None:
            src = img.attributes.get("src") or ""
            if src:
                return src
    return ""


def _title_from_links(node) -> str:
    """Title text from the card's own product link.

    The class that wraps the title is hashed per deploy, so the last resort is
    structural: the first non-decorative product link whose text is not a price.

    On a deals card that link wraps the entire card, so its text runs on into
    the badge and both prices. Everything from the first furniture marker on is
    dropped -- an empty title is a fair answer, "89% off...₹22.00₹2200M.R.P:"
    rendered as a product name is not.
    """
    for link in node.css("a"):
        if link.attributes.get("aria-hidden") == "true":
            continue
        if "/dp/" not in (link.attributes.get("href") or ""):
            continue
        text = _clean_text(link.text(strip=True))
        cut = _TITLE_FURNITURE.search(text)
        if cut is not None:
            text = text[: cut.start()].strip()
        if text and not _PRICE_ONLY.match(text):
            return text
    return ""


# ---------------------------------------------------------------------------
# /deals
# ---------------------------------------------------------------------------


def _deal_prices(card) -> tuple[int, int]:
    """``(price, mrp)`` in paise for one deal card.

    The explicit ``dcl-`` classes are tried first, then a generic sweep of every
    ``a-price`` block partitioned by strike-through. The sweep is the part that
    matters: if Amazon renames ``dcl-product-price-new``, we still find the sale
    price, and we still refuse to let the struck M.R.P. stand in for it.
    """
    price = _price_from_block(card.css_first("span.dcl-product-price-new"))
    mrp = _price_from_block(card.css_first("span.dcl-product-price-old"))

    if not price or not mrp:
        for node in card.css("span.a-price"):
            if _is_struck_price(node):
                if not mrp:
                    mrp = _price_from_block(node)
            elif not price:
                price = _price_from_block(node)

    # A "sale price" read off a struck node is the classic silent-wrong-number
    # bug; sane_mrp is the last gate, dropping an M.R.P. that is not above it.
    return price, money.sane_mrp(mrp, price)


def _deal_badge(card) -> tuple[str, str]:
    """``(discount_label, badge_message)`` -- e.g. ``("89% off", "Mega Deal")``."""
    holder = card.css_first('[data-component="dui-badge"]') or card.css_first("div.dcl-badge")
    if holder is None:
        return "", ""
    discount = badge = ""
    for span in holder.css("span"):
        text = _clean_text(span.text(strip=True))
        if not text:
            continue
        if "%" in text:
            if not discount:
                discount = text
        elif not badge:
            badge = text
    return discount, badge


def parse_deals(html: str) -> list[Deal]:
    """Parse the server-rendered deal cards from ``/deals``.

    Returns them in page order. Cards without an ASIN, or with neither a title
    nor a price, are dropped: they are placeholders for content the page fills
    in from JavaScript we never run.
    """
    _reject_bot_check(html)
    tree = HTMLParser(html)

    deals: list[Deal] = []
    seen: set[str] = set()
    for card in tree.css("div.dcl-product"):
        link = card.css_first("a.dcl-product-link") or card.css_first("a[href]")
        asin = _asin_from_href(link.attributes.get("href", "") if link is not None else "")
        if not _ASIN_RE.match(asin) or asin in seen:
            continue

        label = card.css_first("span.dcl-product-label")
        title = _clean_text(label.text(strip=True)) if label is not None else ""
        if not title:
            title = _title_from_links(card)
        if not title:
            img = card.css_first("img[alt]")
            title = _clean_text(img.attributes.get("alt") or "") if img is not None else ""

        price, mrp = _deal_prices(card)
        if not title and not price:
            continue

        discount, badge = _deal_badge(card)
        seen.add(asin)
        deals.append(Deal(
            asin=asin,
            title=title,
            price=price,
            mrp=mrp,
            discount=discount,
            rank=len(deals) + 1,
            image_url=_first_image(card),
            badge=badge,
        ))

    return deals


def deal_discount(deal: Deal) -> int:
    """Percent off for filtering and sorting.

    Computed from price and M.R.P. when both parsed, because that is the number
    the user can verify from the row itself. Falls back to the badge Amazon
    printed, so a deal whose prices we failed to read is still filtered on
    something real rather than silently treated as 0% off.
    """
    percent = deal.discount_percent
    if percent:
        return percent
    match = _PERCENT_RE.search(deal.discount or "")
    return int(match.group(1)) if match else 0


def filter_deals(deals: list[Deal], min_discount: int = 0, limit: int = 0) -> list[Deal]:
    """Sort by discount descending, drop anything under ``min_discount``, cap at ``limit``.

    The sort is stable, so deals tied on percentage keep Amazon's own ordering.
    ``limit`` is applied last: ``--limit 5 --min-discount 50`` means "five best
    deals that are at least half off", not "of the first five, whichever are".
    """
    selected = [d for d in deals if deal_discount(d) >= min_discount] if min_discount > 0 else list(deals)
    selected.sort(key=deal_discount, reverse=True)
    return selected[:limit] if limit and limit > 0 else selected


async def get_deals(client: AmazonClient) -> list[Deal]:
    """Fetch and parse ``/deals``."""
    return parse_deals(await client.fetch(DEALS_PATH))


# ---------------------------------------------------------------------------
# /gp/bestsellers
# ---------------------------------------------------------------------------


def resolve_category(category: str | None) -> str | None:
    """Normalise a user-supplied department slug, or ``None`` for all departments.

    Raises :class:`~amazon_cli.errors.InputError` naming close matches rather
    than letting Amazon answer a guessed slug with a 404 the user cannot act on.
    """
    if category is None:
        return None
    slug = category.strip().lower().replace(" ", "-").replace("_", "-")
    if not slug:
        return None
    slug = _CATEGORY_ALIASES.get(slug, slug)
    if slug in BESTSELLER_CATEGORIES:
        return slug

    suggestions = difflib.get_close_matches(slug, BESTSELLER_CATEGORIES, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise InputError(
        f"Unknown bestsellers category {category!r}.{hint} "
        f"Run 'amz bestsellers --list-categories' for the full list."
    )


def bestsellers_path(category: str | None = None) -> str:
    """``/gp/bestsellers`` or ``/gp/bestsellers/<slug>``."""
    slug = resolve_category(category)
    return f"{BESTSELLERS_PATH}/{slug}" if slug else BESTSELLERS_PATH


def _bestseller_rank_and_slug(node) -> tuple[int, str]:
    rank = 0
    badge = node.css_first("span.zg-bdg-text")
    if badge is not None:
        match = _RANK_BADGE.search(badge.text(strip=True))
        if match:
            rank = int(match.group(1).replace(",", ""))

    slug = ""
    for link in node.css("a[href]"):
        match = _ZG_REF.search(link.attributes.get("href") or "")
        if match:
            slug = match.group(1)
            if not rank:
                rank = int(match.group(2))
            break
    return rank, slug


def _bestseller_rating(node) -> tuple[float, int]:
    """``(rating, review_count)``, both zero when the card omits them."""
    rating = 0.0
    review_count = 0

    for link in node.css("a[aria-label]"):
        label = link.attributes.get("aria-label") or ""
        match = _RATING_RE.search(label)
        if not match:
            continue
        rating = float(match.group(1))
        count = _RATINGS_COUNT_RE.search(label)
        if count:
            review_count = int(count.group(1).replace(",", ""))
        break

    if not rating:
        alt = node.css_first("span.a-icon-alt")
        if alt is not None:
            match = _RATING_RE.search(alt.text(strip=True))
            if match:
                rating = float(match.group(1))
    if not review_count:
        small = node.css_first("div.a-icon-row span.a-size-small")
        if small is not None:
            review_count = _parse_count(small.text(strip=True))

    return rating, review_count


def _bestseller_price(node) -> int:
    """Sale price in paise. Zero is a real answer here.

    Bestseller cards for products sold only through variations (a phone with
    per-storage pricing, say) genuinely render no price at all, so an unknown
    price is the honest output -- far better than importing a neighbouring
    card's number.
    """
    for selector in ("span.a-color-price", "span[class*='p13n-sc-price']", "span.a-price"):
        for price_node in node.css(selector):
            if _is_struck_price(price_node):
                continue
            price = money.parse_paise(price_node.text(strip=True))
            if price:
                return price
    return money.UNKNOWN


def parse_bestsellers(html: str) -> list[Deal]:
    """Parse a bestsellers page, single-department grid or all-departments carousels.

    ``rank`` is the rank *within its department*: on ``/gp/bestsellers`` that
    means six runs of 1..6, one per carousel, not 1..36. ``badge`` carries the
    department label so those runs stay distinguishable.
    """
    _reject_bot_check(html)
    tree = HTMLParser(html)

    items: list[Deal] = []
    seen: set[tuple[str, str, int]] = set()
    for node in tree.css("div[data-asin]"):
        asin = (node.attributes.get("data-asin") or "").strip().upper()
        if not _ASIN_RE.match(asin):
            continue
        # A p13n faceout or at least a product link -- otherwise this is one of
        # the recommendation shells Amazon sprinkles around the page.
        if node.css_first("div.p13n-sc-uncoverable-faceout") is None:
            if node.css_first("a[href*='/dp/']") is None:
                continue

        rank, slug = _bestseller_rank_and_slug(node)
        key = (asin, slug, rank)
        if key in seen:
            continue
        seen.add(key)

        title_node = node.css_first("div[class*='line-clamp']") or node.css_first("div.p13n-sc-truncate")
        title = _clean_text(title_node.text(strip=True)) if title_node is not None else ""
        if not title:
            title = _title_from_links(node)
        if not title:
            img = node.css_first("img[alt]")
            title = _clean_text(img.attributes.get("alt") or "") if img is not None else ""

        price = _bestseller_price(node)
        if not title and not price:
            continue

        rating, review_count = _bestseller_rating(node)
        items.append(Deal(
            asin=asin,
            title=title,
            price=price,
            rank=rank,
            rating=rating,
            review_count=review_count,
            image_url=_first_image(node),
            badge=BESTSELLER_CATEGORIES.get(slug, slug.replace("-", " ").title()),
        ))

    return items


def sort_bestsellers(items: list[Deal], limit: int = 0) -> list[Deal]:
    """Order by rank, then cap.

    The sort is stable, so on the all-departments page -- where every department
    contributes its own #1 -- the departments keep the order Amazon put them in
    and the output reads as "every department's #1, then every #2".
    """
    ordered = sorted(items, key=lambda d: d.rank or 10**6)
    return ordered[:limit] if limit and limit > 0 else ordered


async def get_bestsellers(client: AmazonClient, category: str | None = None) -> list[Deal]:
    """Fetch and parse a bestsellers page."""
    return parse_bestsellers(await client.fetch(bestsellers_path(category)))
