"""Regression net for the product parser, run against real captured pages.

Eight Amazon.in product pages are checked into `tests/fixtures/`. Their expected
values were produced independently and verified against the live site. If Amazon
changes its markup, these tests fail -- which is the entire point. A scraper
without captured-page tests does not fail when it breaks; it just quietly starts
returning nothing, or worse, the wrong number.
"""

import re

import pytest
from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.client.parser import parse_product_page, parse_search_results

from conftest import PRODUCT_ASINS, load_product

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


@pytest.fixture(scope="module")
def parsed(request):
    """Every fixture parsed once, keyed by ASIN."""
    return {asin: parse_product_page(load_product(asin), asin) for asin in PRODUCT_ASINS}


# ------------------------------------------------------- field-by-field checks

@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_price_matches_the_verified_value_in_paise(asin, parsed, product_expected):
    # expected.json stores whole rupees; the parser now returns paise.
    assert parsed[asin].price == product_expected[asin]["price"] * 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_mrp_matches_the_verified_value_in_paise(asin, parsed, product_expected):
    assert parsed[asin].mrp == product_expected[asin]["mrp"] * 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_title_matches(asin, parsed, product_expected):
    assert parsed[asin].title == product_expected[asin]["title"]


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_rating_and_review_count_match(asin, parsed, product_expected):
    assert parsed[asin].rating == product_expected[asin]["rating"]
    assert parsed[asin].review_count == product_expected[asin]["review_count"]


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_availability_matches_and_carries_no_leaked_json(asin, parsed, product_expected):
    availability = parsed[asin].availability
    assert availability == product_expected[asin]["availability"]
    assert "{" not in availability
    assert len(availability) < 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_image_url_is_a_real_url(asin, parsed):
    url = parsed[asin].image_url
    assert url.startswith("https://")
    assert not url.startswith("data:")


# --------------------------------------------------------------- invariants

@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_price_is_plausible(asin, parsed):
    detail = parsed[asin]
    assert 0 < detail.price <= money.MAX_PAISE
    assert isinstance(detail.price, int)


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_mrp_is_never_at_or_below_the_price(asin, parsed):
    """The guard that stops a carousel price becoming a nonsense discount."""
    detail = parsed[asin]
    assert detail.mrp == 0 or detail.mrp > detail.price
    assert 0 <= detail.discount_pct < 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_discount_percent_agrees_with_amazons_own_badge(asin, parsed):
    """Our computed discount must match the "-26%" Amazon renders, +/-1 for rounding."""
    detail = parsed[asin]
    if not detail.discount or not detail.mrp:
        pytest.skip("no discount badge on this page")
    badge = int(re.sub(r"[^\d]", "", detail.discount))
    assert abs(detail.discount_pct - badge) <= 1


def test_the_no_mrp_product_really_has_no_mrp(parsed):
    # Nike shoes: priced, but no struck-through list price on the page.
    nike = parsed["B0DBVVW9XF"]
    assert nike.price > 0
    assert nike.mrp == 0
    assert nike.discount_pct == 0


def test_the_lakh_scale_product_formats_the_indian_way(parsed):
    macbook = parsed["B0GR177QCS"]
    assert macbook.price == 17_249_000
    assert macbook.price_display == "Rs.1,72,490"
    assert "1,72,490" in macbook.price_display


def test_the_low_stock_product_reports_its_stock_line(parsed):
    bravia = parsed["B0F7X538TC"]
    assert "Only 1 left" in bravia.availability
    assert bravia.in_stock is True


# ------------------------------------------------------------- layout drift

def _strip(html: str, selector: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css(selector):
        node.decompose()
    return tree.html


def test_a_page_with_the_buy_box_removed_never_reports_the_mrp_as_the_price():
    """The failure mode that matters most: a plausible, non-zero, wrong price.

    With the buy box gone, a naive parser falls through to the struck-through
    M.R.P. node and returns Rs.34,990 for a Rs.25,990 product -- and nothing
    downstream can tell that apart from a real price.
    """
    html = load_product("B0BZP2H373")
    reference = parse_product_page(html, "B0BZP2H373")
    assert reference.mrp > 0

    stripped = _strip(
        html,
        "div#corePrice_feature_div, div#corePriceDisplay_desktop_feature_div, span.priceToPay",
    )
    detail = parse_product_page(stripped, "B0BZP2H373")
    assert detail.price != reference.mrp
    assert detail.price in (0, reference.price)


@pytest.mark.parametrize(
    "selector",
    [
        "span#productTitle",
        "div#availability",
        "div#acrPopover",
        "img#landingImage",
        "span.basisPrice",
        "div#feature-bullets",
    ],
)
def test_removing_any_single_block_degrades_instead_of_crashing(selector):
    html = _strip(load_product("B0BZP2H373"), selector)
    detail = parse_product_page(html, "B0BZP2H373")
    assert detail.asin == "B0BZP2H373"
    assert detail.price == 0 or 0 < detail.price <= money.MAX_PAISE
    assert detail.mrp == 0 or detail.mrp > detail.price


@pytest.mark.parametrize(
    "html",
    ["", "   ", "not html at all", "<html><body><p>nothing here</p></body></html>"],
)
def test_junk_input_yields_an_empty_detail_rather_than_an_exception(html):
    detail = parse_product_page(html, "B0BZP2H373")
    assert detail.asin == "B0BZP2H373"
    assert detail.price == 0
    assert detail.title == ""


def test_a_truncated_page_does_not_crash():
    html = load_product("B0BZP2H373")[:5000]
    detail = parse_product_page(html, "B0BZP2H373")
    assert detail.price == 0 or detail.price > 0  # the point is: no exception


# ------------------------------------------------------------------- search

def test_search_results_parse_from_a_captured_page(search_page):
    products, total = parse_search_results(search_page)
    assert len(products) >= 10
    assert total >= 0
    for product in products:
        assert ASIN_RE.match(product.asin)
        assert product.title
        assert product.price == 0 or 0 < product.price <= money.MAX_PAISE
        assert isinstance(product.price, int)


def test_search_prices_are_paise_not_rupees(search_page):
    products, _ = parse_search_results(search_page)
    priced = [p for p in products if p.price]
    assert priced, "captured search page had no priced results"
    # Every price is a whole number of paise, so all end in at least two digits
    # of rupees*100 -- a rupee value would be 100x too small to render correctly.
    for product in priced:
        assert product.price_display.startswith("Rs.")
        assert money.parse_paise(product.price_display) == product.price


def test_search_on_junk_returns_nothing_rather_than_raising():
    products, total = parse_search_results("<html></html>")
    assert products == []
    assert total == 0
