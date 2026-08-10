"""Regression net for `amz offers`, run against real captured pages.

Two offer-listing captures and the eight product pages are checked into
`tests/fixtures/`. Everything asserted about money here is an exact paise
integer read out of those pages, because the failure mode that matters for this
command is not "no price" -- it is *a plausible wrong price*, and the most
plausible wrong price on an Amazon page is the struck-through M.R.P. sitting a
few DOM nodes away from the one you would actually pay.

The layout-drift tests mutate a real capture (delete the buy box, the seller,
the delivery block, the twister) and assert the parser degrades to empty or to
an honest blank field. They are what stops a future selector change from turning
a missing node into someone else's number.
"""

import csv
import io
import json
import re

import httpx
import pytest
import respx
from click.testing import CliRunner
from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.cli import cli
from amazon_cli.client.base import AmazonClient
from amazon_cli.client.offers import (
    _availability,
    _delivery,
    _labelled_fields,
    get_offers,
    parse_offers,
    sort_offers,
)
from amazon_cli.client.types import Offer
from amazon_cli.errors import BotCheckError, NotFoundError, RateLimitedError
from amazon_cli.commands.offers import CSV_HEADERS, PLAIN_HEADERS

from conftest import PRODUCT_ASINS, load_fixture, load_product

#: The two captured offer-listing pages and the values verified by hand from
#: the HTML: exact paise, and the buy-box seller.
OFFER_FIXTURES = {
    "offers_B0BZP2H373": {
        "price": 2599000,  # Rs.25,990.00
        "mrp": 3499000,  # struck-through Rs.34,990 -- must never be the price
        "seller": "Clicktech Retail Private Ltd",
        "ships_from": "Amazon",
    },
    "offers_B00BX5FOCK": {
        "price": 89900,  # Rs.899.00
        "mrp": 110000,  # struck-through Rs.1,100
        "seller": "Clicktech Retail Private Ltd",
        "ships_from": "Amazon",
    },
}

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

#: A minimal buy box, used where a 1 MB capture would only slow the test down.
BUY_BOX_HTML = """<html><body>
<div id="corePrice_feature_div">
  <span class="a-price"><span class="a-offscreen">&#8377;1,299.00</span></span>
</div>
<div class="offer-display-feature-text">
  Ships fromAmazonAmazonSold byTest Seller Pvt LtdTest Seller Pvt Ltd
</div>
</body></html>"""

NO_PRICE_HTML = "<html><body><h1>Nothing for sale here</h1></body></html>"


@pytest.fixture(scope="module")
def offer_pages() -> dict[str, str]:
    return {name: load_fixture(name) for name in OFFER_FIXTURES}


@pytest.fixture
def mock():
    """A respx router bound to amazon.in.

    Nothing in this file is allowed to touch the real network: an unrouted
    request raises inside respx rather than leaving the machine.
    """
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as router:
        yield router


def strip_nodes(html: str, *selectors: str) -> str:
    """The same page with every node matching `selectors` deleted.

    This is how layout drift is simulated: Amazon does not announce a markup
    change, it just stops rendering a node we relied on.
    """
    tree = HTMLParser(html)
    for selector in selectors:
        for node in tree.css(selector):
            node.decompose()
    return tree.html


def offer(price=0, shipping=0, **kw) -> Offer:
    return Offer(price=price, shipping=shipping, **kw)


# --------------------------------------------------------------- money is exact


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_captured_page_yields_exactly_one_buy_box_offer(name, offer_pages):
    assert len(parse_offers(offer_pages[name])) == 1


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_price_is_the_exact_paise_in_the_buy_box(name, offer_pages):
    [found] = parse_offers(offer_pages[name])
    assert found.price == OFFER_FIXTURES[name]["price"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_price_is_never_the_struck_through_mrp(name, offer_pages):
    expected = OFFER_FIXTURES[name]
    [found] = parse_offers(offer_pages[name])
    assert found.price != expected["mrp"]
    assert found.price < expected["mrp"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_the_mrp_really_is_on_the_page_so_the_test_above_has_teeth(name, offer_pages):
    node = HTMLParser(offer_pages[name]).css_first("span.basisPrice span.a-offscreen")
    assert node is not None
    assert money.parse_paise(node.text(strip=True)) == OFFER_FIXTURES[name]["mrp"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_seller_and_shipper_come_out_unduplicated(name, offer_pages):
    expected = OFFER_FIXTURES[name]
    [found] = parse_offers(offer_pages[name])
    assert found.seller == expected["seller"]
    assert found.ships_from == expected["ships_from"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_buy_box_offer_is_new_free_shipping_and_totals_to_the_price(name, offer_pages):
    [found] = parse_offers(offer_pages[name])
    assert found.condition == "New"
    assert found.shipping == 0
    assert found.total == found.price


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_delivery_promise_is_captured_and_bounded(name, offer_pages):
    [found] = parse_offers(offer_pages[name])
    assert "delivery" in found.delivery.lower()
    assert len(found.delivery) <= 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_product_pages_agree_with_the_independently_verified_price(asin, product_expected):
    """The `/dp/` fallback must produce the same number the product parser does.

    `product_expected.json` was verified against the live site, so this ties the
    offers parser to an outside source of truth rather than to itself.
    """
    [found] = parse_offers(load_product(asin))
    assert found.price == product_expected[asin]["price"] * 100
    if product_expected[asin]["mrp"]:
        assert found.price != product_expected[asin]["mrp"] * 100


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_product_pages_name_a_seller_and_a_shipper(asin):
    [found] = parse_offers(load_product(asin))
    assert found.seller
    assert found.ships_from


# ------------------------------------------- only the buy box is server-rendered


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_no_marketplace_rows_are_server_rendered(name, offer_pages):
    """The module docstring's central claim, pinned.

    If Amazon ever server-renders `#aod-offer` rows again this fails, which is
    the point: the command's "only the buy box" caveat would then be a lie.
    """
    tree = HTMLParser(offer_pages[name])
    assert tree.css("div#aod-offer") == []
    assert tree.css("div.olpOffer") == []
    assert "aod-offer" not in offer_pages[name].lower()


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_the_all_offers_panel_is_an_empty_shell(name, offer_pages):
    """`#all-offers-display` exists but carries no offer -- it is a spinner."""
    node = HTMLParser(offer_pages[name]).css_first("#all-offers-display")
    assert node is not None
    assert len(node.html) < 2_000
    assert money.parse_paise(node.text(strip=True)) == 0


# ------------------------------------------------------------------ layout drift

DRIFT_CASES = {
    "seller block gone": ("div[class*='offer-display-feature']", "#sellerProfileTriggerId"),
    "availability gone": ("div#availability",),
    "delivery block gone": (
        "div#mir-layout-DELIVERY_BLOCK",
        "div#deliveryBlockMessage",
        "span[data-csa-c-delivery-time]",
    ),
    "twister gone": ("div#twister_feature_div", "#twister"),
    "buybox container gone": ("div#buybox", "#desktop_buybox"),
    "data-asin attributes gone": ("li[data-asin]",),
    "prime icon gone": ("i.a-icon-prime",),
}


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
@pytest.mark.parametrize("case", sorted(DRIFT_CASES))
def test_drift_never_crashes_and_never_invents_a_price(case, name, offer_pages):
    expected = OFFER_FIXTURES[name]
    mutated = strip_nodes(offer_pages[name], *DRIFT_CASES[case])
    found = parse_offers(mutated)
    assert len(found) <= 1
    for item in found:
        # Either the right price or none at all -- never the M.R.P., never junk.
        assert item.price == expected["price"]
        assert item.total >= item.price


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_removing_the_seller_blanks_it_rather_than_inventing_one(name, offer_pages):
    mutated = strip_nodes(
        offer_pages[name], "div[class*='offer-display-feature']", "#sellerProfileTriggerId"
    )
    [found] = parse_offers(mutated)
    assert found.seller == ""
    assert found.ships_from == ""
    assert found.price == OFFER_FIXTURES[name]["price"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_removing_the_delivery_block_blanks_the_promise(name, offer_pages):
    mutated = strip_nodes(
        offer_pages[name],
        "div#mir-layout-DELIVERY_BLOCK",
        "div#deliveryBlockMessage",
        "span[data-csa-c-delivery-time]",
    )
    [found] = parse_offers(mutated)
    assert found.delivery == ""
    assert found.price == OFFER_FIXTURES[name]["price"]


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_removing_the_buy_box_does_not_fall_through_to_the_mrp(name, offer_pages):
    """The one that matters most.

    With the payable price deleted, the struck-through M.R.P. is still on the
    page and is still matched by one of the price selectors. Returning it would
    be a plausible, non-zero, *wrong* number -- worse than returning nothing.
    """
    expected = OFFER_FIXTURES[name]
    mutated = strip_nodes(offer_pages[name], "div#corePrice_feature_div", "span.priceToPay")

    # The M.R.P. survived the mutation, so the parser really is being tempted.
    surviving = HTMLParser(mutated).css_first("span.basisPrice span.a-offscreen")
    assert surviving is not None
    assert money.parse_paise(surviving.text(strip=True)) == expected["mrp"]

    assert parse_offers(mutated) == []


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_stripping_every_price_node_yields_no_offer(name, offer_pages):
    mutated = strip_nodes(offer_pages[name], "span.a-offscreen", "span.a-price-whole")
    assert parse_offers(mutated) == []


# --------------------------------------------------------- the marketplace path

#: Modelled on Amazon's "other sellers" rows. There is no captured fixture for
#: this -- the endpoint is unreachable unauthenticated -- but `parse_offers`
#: promises in its docstring to pick these up the day they come back, so the
#: promise is tested.
AOD_HTML = """<html><body><div id="all-offers-display">
  <div id="aod-offer">
    <div id="aod-offer-heading"><h5>New</h5></div>
    <div id="aod-offer-price">
      <span class="a-price"><span class="a-offscreen">&#8377;1,299.00</span></span>
    </div>
    <div id="aod-offer-soldBy"><a href="/sp?seller=A1B2">Cloudtail India</a></div>
  </div>
  <div id="aod-offer">
    <div id="aod-offer-heading"><h5>Used - Very Good</h5></div>
    <div id="aod-offer-price">
      <span class="a-price"><span class="a-offscreen">&#8377;999.00</span></span>
    </div>
    <div id="aod-offer-soldBy"><a href="/sp?seller=C3D4">Second Hand Books</a></div>
  </div>
</div>
<div id="corePrice_feature_div">
  <span class="a-price"><span class="a-offscreen">&#8377;1,499.00</span></span>
</div>
</body></html>"""


def test_marketplace_rows_win_over_the_buy_box():
    found = parse_offers(AOD_HTML)
    assert [o.price for o in found] == [129900, 99900]
    assert [o.seller for o in found] == ["Cloudtail India", "Second Hand Books"]
    assert [o.condition for o in found] == ["New", "Used - Very Good"]


def test_a_rows_own_sub_sections_are_not_counted_as_extra_offers():
    """`#aod-offer-price` is a *part* of a row, not a row.

    Matching it as one duplicated every offer, and the duplicates carried no
    seller and the wrong condition -- a used listing reappeared as "New".
    """
    found = parse_offers(AOD_HTML)
    assert len(found) == 2
    assert all(o.seller for o in found)


def test_a_row_with_no_price_node_is_skipped_not_guessed():
    """A row whose only number is a star rating must not become Rs.4.50."""
    html = """<html><body><div id="aod-offer">
      <div id="aod-offer-heading"><h5>Used</h5></div>
      <span class="a-icon-alt">4.5 out of 5 stars</span>
      <div>Currently unavailable</div>
    </div></body></html>"""
    assert parse_offers(html) == []


def test_a_numbered_row_id_still_counts_as_a_row():
    html = """<html><body><div id="aod-offer-3">
      <div id="aod-offer-price"><span class="a-offscreen">&#8377;700</span></div>
    </div></body></html>"""
    assert [o.price for o in parse_offers(html)] == [70000]


def test_the_legacy_olp_row_class_still_counts_as_a_row():
    html = """<html><body><div class="olpOffer">
      <span class="a-offscreen">&#8377;500.00</span>
      <a href="/x?seller=Z">Shop Z</a>
    </div></body></html>"""
    [found] = parse_offers(html)
    assert found.price == 50000
    assert found.seller == "Shop Z"


# --------------------------------------------------------------- hostile input

HOSTILE = {
    "empty": "",
    "whitespace": "   \n\t  ",
    "plain text": "this is not html, it is an apology",
    "unclosed tag soup": "<div><span><p>&#8377;99",
    "deeply nested junk": "<div>" * 500 + "&#8377;42" + "</div>" * 500,
    "json": '{"price": 12345}',
    "null byte": "\x00\x00\x00",
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_input_never_raises(case):
    assert isinstance(parse_offers(HOSTILE[case]), list)


def test_none_is_treated_as_an_empty_page():
    assert parse_offers(None) == []


@pytest.mark.parametrize("cut", [500, 5_000, 50_000, 200_000])
@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_a_truncated_page_never_raises_and_never_lies(cut, name, offer_pages):
    found = parse_offers(offer_pages[name][:cut])
    assert all(o.price == OFFER_FIXTURES[name]["price"] for o in found)


def test_the_bot_check_page_parses_to_nothing(botcheck_page):
    """Empty, not wrong -- and the client layer is what must raise for it.

    See `test_bot_check_during_fetch_propagates`: an interstitial has to become
    a `BotCheckError`, never a quiet "no offers".
    """
    assert parse_offers(botcheck_page) == []


# ------------------------------------------------------------- field extraction


def test_labelled_fields_undoubles_the_repeated_value():
    tree = HTMLParser(
        '<div class="offer-display-feature-text">'
        "Ships fromAmazonAmazonSold byRetailEZ Pvt LtdRetailEZ Pvt Ltd</div>"
    )
    assert _labelled_fields(tree) == {"ships from": "Amazon", "sold by": "RetailEZ Pvt Ltd"}


def test_labelled_fields_falls_back_to_the_seller_profile_link():
    tree = HTMLParser('<a id="sellerProfileTriggerId">Solo Seller</a>')
    assert _labelled_fields(tree) == {"sold by": "Solo Seller"}


def test_labelled_fields_on_an_empty_page_invents_nothing():
    assert _labelled_fields(HTMLParser("<html></html>")) == {}


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_availability_is_read_cleanly_off_the_page(name, offer_pages):
    """`_availability` is parsed but has nowhere to go -- `Offer` has no field
    for it. Pinned here so the helper cannot rot unnoticed."""
    text = _availability(HTMLParser(offer_pages[name]))
    assert text == "In stock"
    assert "{" not in text


def test_availability_and_delivery_are_blank_when_absent():
    tree = HTMLParser("<html><body></body></html>")
    assert _availability(tree) == ""
    assert _delivery(tree) == ""


# --------------------------------------------------------------------- sorting


def test_total_beats_price_when_shipping_breaks_the_tie():
    """The whole reason `total` is the default sort key."""
    cheap_item_pricey_delivery = offer(price=100_000, shipping=5_000, seller="A")
    dearer_item_free_delivery = offer(price=102_000, shipping=0, seller="B")
    both = [cheap_item_pricey_delivery, dearer_item_free_delivery]

    assert [o.seller for o in sort_offers(both, "price")] == ["A", "B"]
    assert [o.seller for o in sort_offers(both, "total")] == ["B", "A"]


def test_total_is_the_default_key():
    both = [offer(price=100_000, shipping=5_000, seller="A"), offer(price=102_000, seller="B")]
    assert sort_offers(both) == sort_offers(both, "total")


def test_price_sort_is_ascending():
    ordered = sort_offers([offer(price=p) for p in (300, 100, 200)], "price")
    assert [o.price for o in ordered] == [100, 200, 300]


def test_offers_with_an_unknown_price_sort_last_not_first():
    ordered = sort_offers([offer(price=0), offer(price=500), offer(price=100)], "price")
    assert [o.price for o in ordered] == [100, 500, 0]


def test_offers_with_an_unknown_total_sort_last_not_first():
    ordered = sort_offers([offer(price=0, shipping=900), offer(price=500)], "total")
    assert [o.price for o in ordered] == [500, 0]


def test_rating_sort_compares_the_number_not_the_string():
    """Lexicographically "95% positive" > "100% positive". Numerically it is not."""
    ordered = sort_offers(
        [
            offer(price=100, seller="ninety-five", seller_rating="95% positive"),
            offer(price=100, seller="hundred", seller_rating="100% positive"),
        ],
        "rating",
    )
    assert [o.seller for o in ordered] == ["hundred", "ninety-five"]


def test_rating_sort_puts_unrated_sellers_last():
    ordered = sort_offers(
        [
            offer(price=100, seller="unrated"),
            offer(price=100, seller="rated", seller_rating="3.9 out of 5"),
        ],
        "rating",
    )
    assert [o.seller for o in ordered] == ["rated", "unrated"]


def test_rating_sort_of_equally_rated_sellers_prefers_the_cheaper_total():
    ordered = sort_offers(
        [
            offer(price=900, shipping=100, seller="dear", seller_rating="4.5"),
            offer(price=700, seller="cheap", seller_rating="4.5"),
        ],
        "rating",
    )
    assert [o.seller for o in ordered] == ["cheap", "dear"]


def test_sorting_an_empty_list_is_empty_not_an_error():
    for key in ("total", "price", "rating", "nonsense"):
        assert sort_offers([], key) == []


def test_an_unknown_sort_key_falls_back_to_total():
    both = [offer(price=200), offer(price=100)]
    assert sort_offers(both, "nonsense") == sort_offers(both, "total")


def test_sorting_does_not_mutate_the_input():
    original = [offer(price=300), offer(price=100)]
    snapshot = list(original)
    sort_offers(original, "price")
    assert original == snapshot


@pytest.mark.parametrize("name", sorted(OFFER_FIXTURES))
def test_total_never_undercuts_price_on_a_real_page(name, offer_pages):
    for found in parse_offers(offer_pages[name]):
        assert found.total >= found.price
        assert found.total == found.price + found.shipping


@pytest.mark.parametrize("shipping", [0, 1, 9_999_99])
def test_total_is_price_plus_shipping_whenever_the_price_is_known(shipping):
    assert offer(price=50_000, shipping=shipping).total == 50_000 + shipping


def test_total_is_unknown_rather_than_shipping_only_when_the_price_is_unknown():
    """A shipping charge with no item price is not a total anyone can pay."""
    assert offer(price=0, shipping=9_900).total == 0


# --------------------------------------------------------------- network layer


async def test_offer_listing_path_is_requested_first(mock):
    route = mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=BUY_BOX_HTML)
    )
    async with AmazonClient(max_retries=0) as client:
        found = await get_offers(client, "B0BZP2H373")

    assert route.call_count == 1
    assert [o.price for o in found] == [129900]


async def test_a_lowercase_asin_is_normalised_into_the_path(mock):
    route = mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=BUY_BOX_HTML)
    )
    async with AmazonClient(max_retries=0) as client:
        await get_offers(client, "  b0bzp2h373 ")
    assert route.call_count == 1


async def test_the_documented_fallback_to_the_product_page_really_happens(mock):
    """`/gp/offer-listing/` yielding nothing must be retried as `/dp/`."""
    listing = mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=NO_PRICE_HTML)
    )
    product = mock.get("/dp/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=BUY_BOX_HTML)
    )

    async with AmazonClient(max_retries=0) as client:
        found = await get_offers(client, "B0BZP2H373")

    assert listing.call_count == 1
    assert product.call_count == 1
    assert [o.price for o in found] == [129900]


async def test_no_fallback_request_when_the_first_page_already_had_the_offer(mock):
    mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=BUY_BOX_HTML)
    )
    product = mock.get("/dp/B0BZP2H373").mock(return_value=httpx.Response(200, text=BUY_BOX_HTML))

    async with AmazonClient(max_retries=0) as client:
        await get_offers(client, "B0BZP2H373")

    assert product.call_count == 0


async def test_both_pages_empty_is_an_empty_list_not_an_error(mock):
    mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=NO_PRICE_HTML)
    )
    mock.get("/dp/B0BZP2H373").mock(return_value=httpx.Response(200, text=NO_PRICE_HTML))

    async with AmazonClient(max_retries=0) as client:
        assert await get_offers(client, "B0BZP2H373") == []


async def test_a_404_surfaces_as_not_found(mock):
    mock.get("/gp/offer-listing/B0BZP2H373").mock(return_value=httpx.Response(404))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(NotFoundError):
            await get_offers(client, "B0BZP2H373")


async def test_a_404_on_the_fallback_also_surfaces_as_not_found(mock):
    mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=NO_PRICE_HTML)
    )
    mock.get("/dp/B0BZP2H373").mock(return_value=httpx.Response(404))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(NotFoundError):
            await get_offers(client, "B0BZP2H373")


async def test_bot_check_during_fetch_propagates(mock):
    """A captcha must never be laundered into "this product has no offers"."""
    mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text="<html>Enter the characters you see below</html>")
    )
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(BotCheckError) as excinfo:
            await get_offers(client, "B0BZP2H373")
    assert excinfo.value.exit_code == 5


async def test_throttling_surfaces_as_rate_limited(mock):
    mock.get("/gp/offer-listing/B0BZP2H373").mock(return_value=httpx.Response(429))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(RateLimitedError) as excinfo:
            await get_offers(client, "B0BZP2H373")
    assert excinfo.value.exit_code == 5


async def test_a_malformed_asin_never_reaches_the_network(mock):
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(ValueError):
            await get_offers(client, "nope")
    assert mock.calls.call_count == 0


async def test_a_real_captured_page_survives_a_round_trip_through_the_client(mock, offer_pages):
    """The 1 MB capture must not trip the bot-check heuristic on its way in."""
    mock.get("/gp/offer-listing/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=offer_pages["offers_B0BZP2H373"])
    )
    async with AmazonClient(max_retries=0) as client:
        found = await get_offers(client, "B0BZP2H373")
    assert [o.price for o in found] == [2599000]


# ------------------------------------------------------------------------- CLI


def run_cli(args, html=BUY_BOX_HTML, asin="B0BZP2H373", status=200):
    """Invoke `amz` through the group, with the network mocked out."""
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as mock:
        response = httpx.Response(status, text=html)
        mock.get(f"/gp/offer-listing/{asin}").mock(return_value=response)
        mock.get(f"/dp/{asin}").mock(return_value=response)
        return CliRunner().invoke(cli, args)


def test_cli_renders_the_table_without_a_limit_flag():
    """`amz offers ASIN` with no options. This exited 2 on a rejected default."""
    result = run_cli(["offers", "B0BZP2H373"])
    assert result.exit_code == 0, result.output
    assert "Buying options" in result.output
    assert "Rs.1,299" in result.output


def test_cli_says_so_when_only_the_buy_box_is_available():
    result = run_cli(["offers", "B0BZP2H373"])
    assert "AJAX" in result.output


def test_cli_json_is_valid_and_carries_paise():
    result = run_cli(["offers", "B0BZP2H373", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["price_paise"] for row in payload] == [129900]
    assert payload[0]["total_paise"] == payload[0]["price_paise"] + payload[0]["shipping_paise"]
    assert payload[0]["price"] == 1299


def test_cli_json_of_a_page_with_nothing_for_sale_is_an_empty_array():
    result = run_cli(["offers", "B0BZP2H373", "--json"], html=NO_PRICE_HTML)
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_cli_csv_round_trips_with_a_matching_header_width():
    result = run_cli(["offers", "B0BZP2H373", "--csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    assert rows[0] == CSV_HEADERS
    assert len(rows) == 2
    assert all(len(row) == len(CSV_HEADERS) for row in rows)

    record = dict(zip(rows[0], rows[1]))
    assert record["price_paise"] == "129900"
    assert record["seller"] == "Test Seller Pvt Ltd"


def test_cli_csv_quotes_a_seller_name_containing_a_comma():
    html = BUY_BOX_HTML.replace("Test Seller Pvt Ltd", "Foo, Bar & Co")
    result = run_cli(["offers", "B0BZP2H373", "--csv"], html=html)
    rows = list(csv.reader(io.StringIO(result.output)))
    assert len(rows) == 2
    assert len(rows[1]) == len(CSV_HEADERS)
    assert dict(zip(rows[0], rows[1]))["seller"] == "Foo, Bar & Co"


def test_cli_plain_is_tab_separated_with_the_documented_headers():
    # Deliberately not `.strip()`ed: an empty trailing field (no delivery
    # promise) is still a field, and stripping it would hide a lost column.
    lines = run_cli(["offers", "B0BZP2H373", "--plain"]).output.splitlines()
    assert lines[0].split("\t") == PLAIN_HEADERS
    assert len(lines) == 2
    assert len(lines[1].split("\t")) == len(PLAIN_HEADERS)


def test_cli_plain_reports_paise_not_rupees():
    """`--plain` predates the rupee columns; its `price` is paise by contract."""
    lines = run_cli(["offers", "B0BZP2H373", "--plain"]).output.splitlines()
    record = dict(zip(lines[0].split("\t"), lines[1].split("\t")))
    assert record["price"] == "129900"
    assert record["total"] == "129900"


def test_cli_limit_truncates_the_marketplace_rows():
    result = run_cli(["offers", "B0BZP2H373", "--limit", "1", "--json"], html=AOD_HTML)
    assert [row["price_paise"] for row in json.loads(result.output)] == [99900]


def test_cli_without_a_limit_shows_every_row():
    result = run_cli(["offers", "B0BZP2H373", "--json"], html=AOD_HTML)
    assert len(json.loads(result.output)) == 2


def test_cli_rejects_a_zero_limit():
    result = run_cli(["offers", "B0BZP2H373", "--limit", "0"])
    assert result.exit_code == 2


def test_cli_new_only_drops_used_and_renewed_rows():
    result = run_cli(["offers", "B0BZP2H373", "--new-only", "--json"], html=AOD_HTML)
    payload = json.loads(result.output)
    assert [row["condition"] for row in payload] == ["New"]


def test_cli_new_only_keeps_the_buy_box_offer():
    result = run_cli(["offers", "B0BZP2H373", "--new-only", "--json"])
    assert len(json.loads(result.output)) == 1


def test_cli_sorts_cheapest_first_under_both_money_keys():
    """No parser sets `shipping`, so `total` and `price` agree on a real page.

    They are still both exercised here; `test_total_beats_price_when_shipping
    _breaks_the_tie` covers the case where they diverge.
    """
    by_total = run_cli(["offers", "B0BZP2H373", "--json"], html=AOD_HTML)
    by_price = run_cli(["offers", "B0BZP2H373", "--sort", "price", "--json"], html=AOD_HTML)
    assert [r["price_paise"] for r in json.loads(by_total.output)] == [99900, 129900]
    assert [r["price_paise"] for r in json.loads(by_price.output)] == [99900, 129900]


def test_cli_sort_rating_is_accepted():
    result = run_cli(["offers", "B0BZP2H373", "--sort", "rating", "--json"], html=AOD_HTML)
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)) == 2


def test_cli_rejects_an_unknown_sort_key():
    result = run_cli(["offers", "B0BZP2H373", "--sort", "vibes"])
    assert result.exit_code == 2


@pytest.mark.parametrize("bad", ["notanasin", "B0BZP2H37", "B0BZP2H3733", "", "B0BZP-H373"])
def test_cli_rejects_a_malformed_asin_with_exit_two(bad):
    result = CliRunner().invoke(cli, ["offers", bad])
    assert result.exit_code == 2
    assert "ASIN" in result.output


def test_cli_reports_an_empty_page_and_still_exits_zero():
    result = run_cli(["offers", "B0BZP2H373"], html=NO_PRICE_HTML)
    assert result.exit_code == 0
    assert "No buying options" in result.output


def test_cli_exit_code_for_a_missing_product_is_four():
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as mock:
        mock.get("/gp/offer-listing/B0BZP2H373").mock(return_value=httpx.Response(404))
        result = CliRunner().invoke(cli, ["offers", "B0BZP2H373"])
    assert result.exit_code == 4


def test_cli_exit_code_for_a_bot_check_is_five():
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as mock:
        mock.get("/gp/offer-listing/B0BZP2H373").mock(
            return_value=httpx.Response(
                200, text="<html>Enter the characters you see below</html>"
            )
        )
        result = CliRunner().invoke(cli, ["--retries", "0", "offers", "B0BZP2H373"])
    assert result.exit_code == 5


def test_cli_renders_a_real_captured_page(offer_pages):
    result = run_cli(["offers", "B0BZP2H373"], html=offer_pages["offers_B0BZP2H373"])
    assert result.exit_code == 0, result.output
    assert "Rs.25,990" in result.output
    assert "Rs.34,990" not in result.output  # the M.R.P. is not a buying option
