"""`amz deals` -- parsing, filtering, sorting and the CLI surface.

Everything here runs against `tests/fixtures/deals.html.gz`, a real /deals page
captured during the Amazon Great Freedom Sale 2026. Where a failure mode cannot
be reached from healthy markup (Amazon renaming a strike marker, blanking a
price, dropping the ASIN), the fixture is *mutated* rather than replaced by
hand-written HTML: the mutation starts from markup Amazon actually shipped, so
the test still exercises the real surrounding structure.
"""

import csv
import io
import json

import httpx
import pytest
import respx
from click.testing import CliRunner
from selectolax.parser import HTMLParser

from amazon_cli import money
from amazon_cli.cli import cli
from amazon_cli.client.base import AmazonClient
from amazon_cli.client.deals import (
    DEALS_PATH,
    deal_discount,
    filter_deals,
    get_deals,
    parse_deals,
)
from amazon_cli.client.types import Deal
from amazon_cli.errors import BotCheckError, RateLimitedError

from conftest import load_fixture

BASE = "https://www.amazon.in"
DEALS_URL = f"{BASE}{DEALS_PATH}"

#: The first card on the captured page, read out of the fixture by hand:
#: `<span class="a-offscreen">₹22.00</span>` against a struck `₹199.00`.
FIRST_ASIN = "B08VD9P423"
FIRST_PRICE_PAISE = 22_00
FIRST_MRP_PAISE = 199_00


# --------------------------------------------------------------------- helpers

def _mutate(html: str, *edits) -> str:
    """Apply DOM edits to captured HTML and re-serialise it."""
    tree = HTMLParser(html)
    for edit in edits:
        edit(tree)
    return tree.html


def _drop(selector: str):
    def edit(tree):
        for node in tree.css(selector):
            node.decompose()
    return edit


def _unmark_strikes(tree):
    """Amazon renames its strike markers -- the classic layout drift."""
    for node in tree.css("[data-a-strike]"):
        del node.attrs["data-a-strike"]
    for node in tree.css("[class]"):
        classes = node.attributes.get("class") or ""
        if "a-text-price" in classes:
            node.attrs["class"] = classes.replace("a-text-price", "")


def _paise_from_rupees(text: str) -> int:
    """``'₹1,72,490.00' -> 17249000``, computed here rather than via money.py.

    Deliberately independent of the code under test: if `money.parse_paise` ever
    drifts, this cross-check notices instead of agreeing with it.
    """
    digits = text.replace("₹", "").replace("Rs.", "").replace(",", "").strip()
    whole, _, frac = digits.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


@pytest.fixture(scope="module")
def deals(deals_page):
    return parse_deals(deals_page)


# ------------------------------------------------------------ what the page is

def test_the_captured_page_yields_thirty_eight_deals(deals):
    assert len(deals) == 38


def test_every_server_rendered_card_survives_parsing(deals_page, deals):
    """No card is silently dropped: 38 in the markup, 38 out."""
    assert len(HTMLParser(deals_page).css("div.dcl-product")) == len(deals)


def test_the_first_deal_is_the_twenty_two_rupee_hairband(deals):
    first = deals[0]
    assert first.asin == FIRST_ASIN
    assert first.price == FIRST_PRICE_PAISE
    assert first.mrp == FIRST_MRP_PAISE
    assert first.discount == "89% off"
    assert first.discount_percent == 89
    assert first.rank == 1
    assert first.badge == "Freedom Sale Mega Deal"
    assert first.title.startswith("Trending Trunks Zigzag Wave Metal Hairband")


def test_a_deal_card_carries_no_rating(deals):
    """There is no rating on a /deals card; zero is the honest answer."""
    assert all(d.rating == 0.0 and d.review_count == 0 for d in deals)


# ------------------------------------------------------------------ money is paise

def test_every_price_matches_the_rupee_string_on_its_own_card(deals_page, deals):
    """Cross-check each parsed price against the card's own ``a-offscreen`` text."""
    by_asin = {}
    for card in HTMLParser(deals_page).css("div.dcl-product"):
        link = card.css_first("a.dcl-product-link")
        href = link.attributes.get("href") or ""
        asin = href.split("/dp/")[1][:10]
        new = card.css_first("span.dcl-product-price-new span.a-offscreen")
        old = card.css_first("span.dcl-product-price-old span.a-offscreen")
        by_asin[asin] = (new.text(strip=True), old.text(strip=True))

    assert len(by_asin) == len(deals)
    for deal in deals:
        price_text, mrp_text = by_asin[deal.asin]
        assert deal.price == _paise_from_rupees(price_text), deal.asin
        assert deal.mrp == _paise_from_rupees(mrp_text), deal.asin


def test_prices_are_paise_not_rupees(deals):
    """A hundredth-of-a-rupee unit means Rs.22 is 2200, never 22."""
    assert deals[0].price == 2200
    assert deals[0].price != 22


def test_every_price_is_within_sane_bounds(deals):
    assert all(1 <= d.price <= money.MAX_PAISE for d in deals)
    assert all(1 <= d.mrp <= money.MAX_PAISE for d in deals)


def test_no_deal_reports_its_struck_mrp_as_the_price(deals):
    """The M.R.P. is strictly above the price on every card, so this is provable."""
    assert all(d.price < d.mrp for d in deals)
    assert not any(d.price == d.mrp for d in deals)


def test_the_struck_mrp_never_becomes_the_price_when_the_strike_markers_vanish(deals_page):
    """Regression: Amazon drops `data-a-strike`/`a-text-price` and blanks the sale price.

    Before the fix, the generic ``span.a-price`` sweep read the struck
    ``₹199.00`` as the *sale* price of a Rs.22 hairband -- a plausible,
    non-zero, wrong number, which is exactly the failure mode this parser
    exists to prevent.
    """
    drifted = _mutate(deals_page, _drop("span.dcl-product-price-new"), _unmark_strikes)
    parsed = parse_deals(drifted)

    assert len(parsed) == 38, "cards must survive; only the price is unreadable"
    assert parsed[0].asin == FIRST_ASIN
    assert parsed[0].price == money.UNKNOWN
    assert parsed[0].mrp == FIRST_MRP_PAISE
    assert all(d.price == money.UNKNOWN for d in parsed)


def test_a_struck_only_card_reports_no_price_rather_than_the_list_price(deals_page):
    """Same shape, strike markers left intact: still no invented sale price."""
    parsed = parse_deals(_mutate(deals_page, _drop("span.dcl-product-price-new")))
    assert parsed[0].price == money.UNKNOWN
    assert parsed[0].mrp == FIRST_MRP_PAISE


def test_an_mrp_at_or_below_the_price_is_discarded(deals_page):
    """`sane_mrp` is the last gate: a non-discount must not render as one."""
    def flatten(tree):
        for node in tree.css("span.dcl-product-price-old span.a-offscreen"):
            node.replace_with("<span class='a-offscreen'>₹1.00</span>")

    parsed = parse_deals(_mutate(deals_page, flatten))
    assert parsed[0].price == FIRST_PRICE_PAISE
    assert parsed[0].mrp == money.UNKNOWN
    assert parsed[0].discount_percent == 0


# ------------------------------------------------------------------ ASIN and rank

def test_every_asin_is_ten_alphanumeric_characters(deals):
    assert all(len(d.asin) == 10 and d.asin.isalnum() and d.asin.isupper() for d in deals)


def test_asins_are_unique(deals):
    assert len({d.asin for d in deals}) == len(deals)


def test_rank_records_page_order_one_through_n(deals):
    assert [d.rank for d in deals] == list(range(1, len(deals) + 1))


def test_every_deal_has_a_title_and_an_image(deals):
    assert all(d.title for d in deals)
    assert all(d.image_url.startswith("https://") for d in deals)


# ------------------------------------------------------------ discount arithmetic

def test_discount_percent_is_computed_from_price_and_mrp(deals):
    for deal in deals:
        assert deal.discount_percent == money.discount_percent(deal.price, deal.mrp)


def test_the_computed_discount_agrees_with_the_badge_amazon_printed(deals):
    """A one-point rounding gap is fine; anything wider means we mis-read a price."""
    for deal in deals:
        badge = int(deal.discount.rstrip("% off").strip() or 0)
        assert abs(deal.discount_percent - badge) <= 1, deal.asin


def test_deal_discount_prefers_the_computed_value_over_the_badge():
    deal = Deal(asin="B000000001", title="x", price=5000, mrp=10000, discount="90% off")
    assert deal.discount_percent == 50
    assert deal_discount(deal) == 50


def test_deal_discount_falls_back_to_the_badge_when_the_price_is_unreadable():
    """A deal we failed to price is still filtered on something real, not on 0."""
    deal = Deal(asin="B000000001", title="x", price=0, mrp=0, discount="89% off")
    assert deal.discount_percent == 0
    assert deal_discount(deal) == 89


def test_deal_discount_is_zero_when_there_is_nothing_to_go_on():
    assert deal_discount(Deal(asin="B000000001", title="x")) == 0


# ------------------------------------------------------------- filter and sort

def test_deals_are_sorted_by_discount_descending(deals):
    percents = [deal_discount(d) for d in filter_deals(deals)]
    assert percents == sorted(percents, reverse=True)
    assert percents[0] == 89


def test_ties_keep_amazons_own_page_order(deals):
    """Two 89% deals: the stable sort must not shuffle them."""
    top_two = [d.asin for d in filter_deals(deals)[:2]]
    assert top_two == ["B08VD9P423", "B08K76F3VW"]


def test_filter_does_not_mutate_its_input(deals):
    before = [d.asin for d in deals]
    filter_deals(deals, min_discount=50, limit=3)
    assert [d.asin for d in deals] == before


def test_min_discount_zero_keeps_every_deal(deals):
    assert len(filter_deals(deals, min_discount=0)) == len(deals)


def test_min_discount_is_inclusive_at_the_boundary(deals):
    """53% is a real value on this page; --min-discount 53 must keep it."""
    at_53 = [d for d in deals if deal_discount(d) == 53]
    assert at_53, "fixture no longer has a 53% deal -- pick another boundary"

    kept_53 = filter_deals(deals, min_discount=53)
    kept_54 = filter_deals(deals, min_discount=54)
    assert all(deal_discount(d) >= 53 for d in kept_53)
    assert len(kept_53) - len(kept_54) == len(at_53)
    assert {d.asin for d in at_53} <= {d.asin for d in kept_53}
    assert not {d.asin for d in at_53} & {d.asin for d in kept_54}


def test_min_discount_above_the_best_deal_returns_nothing(deals):
    best = max(deal_discount(d) for d in deals)
    assert filter_deals(deals, min_discount=best + 1) == []


def test_min_discount_one_hundred_returns_nothing(deals):
    assert all(deal_discount(d) < 100 for d in deals)
    assert filter_deals(deals, min_discount=100) == []


@pytest.mark.parametrize("limit,expected", [(1, 1), (5, 5), (38, 38), (39, 38), (1000, 38)])
def test_limit_caps_the_list(deals, limit, expected):
    assert len(filter_deals(deals, limit=limit)) == expected


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_means_no_cap(deals, limit):
    """Pinned, not preferred: the CLI blocks this via IntRange(min=1)."""
    assert len(filter_deals(deals, limit=limit)) == len(deals)


def test_limit_is_applied_after_the_discount_filter(deals):
    """`--limit 5 --min-discount 50` is "the five best deals over 50%"."""
    top = filter_deals(deals, min_discount=50, limit=5)
    assert len(top) == 5
    assert all(deal_discount(d) >= 50 for d in top)
    assert [deal_discount(d) for d in top] == [89, 89, 87, 85, 74]


def test_filtering_an_empty_list_is_empty():
    assert filter_deals([], min_discount=50, limit=10) == []


# ------------------------------------------------------------------ layout drift

def test_renaming_the_card_container_yields_no_deals_rather_than_a_crash(deals_page):
    def rename(tree):
        for node in tree.css("div.dcl-product"):
            node.attrs["class"] = "a-cardui dcl-widget"

    assert parse_deals(_mutate(deals_page, rename)) == []


def test_removing_every_asin_drops_every_card(deals_page):
    def strip_hrefs(tree):
        for node in tree.css("a[href]"):
            node.attrs["href"] = "/"

    assert parse_deals(_mutate(deals_page, strip_hrefs)) == []


def test_blanking_every_price_keeps_the_titles_and_reports_no_price(deals_page):
    parsed = parse_deals(_mutate(deals_page, _drop("span.a-price")))
    assert len(parsed) == 38
    assert all(d.price == money.UNKNOWN and d.mrp == money.UNKNOWN for d in parsed)
    assert all(d.title for d in parsed)
    assert all(d.discount_percent == 0 for d in parsed)


def test_a_priceless_card_is_still_filterable_on_its_badge(deals_page):
    parsed = parse_deals(_mutate(deals_page, _drop("span.a-price")))
    assert deal_discount(parsed[0]) == 89
    assert len(filter_deals(parsed, min_discount=80)) > 0


def test_removing_the_title_never_renders_price_furniture_as_a_product_name(deals_page):
    """Regression: the structural fallback used to return the whole card's text.

    A deals card is wrapped in a single <a>, so `link.text()` is
    ``"<title>89% offFreedom Sale Mega Deal₹22.00₹2200M.R.P:₹199.00"``. With the
    title span gone the title *starts* at the badge, so the honest answer is an
    empty title -- not a product called "89% offFreedom Sale Mega Deal₹22.00".
    """
    parsed = parse_deals(_mutate(deals_page, _drop("span.dcl-product-label")))
    assert len(parsed) == 38, "priced cards are still worth keeping"
    assert parsed[0].asin == FIRST_ASIN
    assert parsed[0].price == FIRST_PRICE_PAISE
    assert all(d.price for d in parsed)
    for deal in parsed:
        assert "% off" not in deal.title
        assert "₹" not in deal.title
        assert "M.R.P" not in deal.title


def test_an_empty_image_alt_does_not_crash_the_parser(deals_page):
    """Regression: `attributes.get("alt", "")` returns None for ``alt=""``.

    selectolax stores an empty attribute as ``None``, so the dict default never
    fires and ``_clean_text(None)`` raised TypeError. Every card on the captured
    page carries ``<img alt="">``, so this is one layout change away from a
    hard crash on the real site.
    """
    stripped = _mutate(deals_page, _drop("span.dcl-product-label"))
    assert 'alt=""' in stripped
    parsed = parse_deals(stripped)  # must not raise
    assert len(parsed) == 38
    assert all(d.title == "" for d in parsed)


def test_removing_the_badge_leaves_the_discount_computed_from_prices(deals_page):
    parsed = parse_deals(_mutate(deals_page, _drop("div.dcl-badge")))
    assert len(parsed) == 38
    assert all(d.discount == "" and d.badge == "" for d in parsed)
    assert parsed[0].discount_percent == 89
    assert deal_discount(parsed[0]) == 89


def test_removing_the_images_never_invents_a_url(deals_page):
    parsed = parse_deals(_mutate(deals_page, _drop("img")))
    assert len(parsed) == 38
    assert all(d.image_url == "" for d in parsed)


# ----------------------------------------------------------------- hostile input

@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "\n\t\n",
        "not html at all",
        "<<<>>>&amp;",
        "<html><body><div class='dcl-product'></div></body></html>",
        "<div class='dcl-product'><a href='/dp/SHORT'>x</a></div>",
        "\x00\x01\x02",
        "<html>" + "₹" * 5000 + "</html>",
    ],
)
def test_hostile_input_returns_an_empty_list_and_never_raises(html):
    assert parse_deals(html) == []


def test_a_truncated_page_degrades_instead_of_raising(deals_page):
    truncated = deals_page[:5000]
    parsed = parse_deals(truncated)
    assert isinstance(parsed, list)
    assert all(len(d.asin) == 10 for d in parsed)


@pytest.mark.parametrize("cut", [500, 5_000, 50_000, 200_000])
def test_every_truncation_point_parses_without_raising(deals_page, cut):
    parsed = parse_deals(deals_page[:cut])
    assert all(d.price >= 0 and d.mrp >= 0 for d in parsed)


def test_the_bot_check_page_raises_rather_than_reporting_no_deals(botcheck_page):
    """"No deals today" for what is really a block is the bug this project kills."""
    with pytest.raises(BotCheckError) as excinfo:
        parse_deals(botcheck_page)
    assert excinfo.value.exit_code == 5


# ----------------------------------------------------------------- client wiring

@respx.mock
async def test_get_deals_requests_the_deals_path(deals_page):
    route = respx.get(DEALS_URL).mock(return_value=httpx.Response(200, text=deals_page))
    async with AmazonClient(max_retries=0) as client:
        found = await get_deals(client)
    assert route.called
    assert len(found) == 38


@respx.mock
async def test_get_deals_propagates_a_bot_check_instead_of_returning_empty(botcheck_page):
    respx.get(DEALS_URL).mock(return_value=httpx.Response(200, text=botcheck_page))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(BotCheckError):
            await get_deals(client)


@respx.mock
async def test_get_deals_propagates_a_throttle():
    respx.get(DEALS_URL).mock(return_value=httpx.Response(429))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(RateLimitedError):
            await get_deals(client)


# -------------------------------------------------------------------------- CLI

def _run(args, page=None, status=200):
    """Invoke the CLI with /deals mocked. No test may reach the real network."""
    runner = CliRunner()
    with respx.mock:
        respx.get(DEALS_URL).mock(
            return_value=httpx.Response(status, text=page if page is not None else "")
        )
        return runner.invoke(cli, ["--retries", "0", "deals", *args])


def _flat(text: str) -> str:
    """Collapse rich's line wrapping so assertions can look for a phrase."""
    return " ".join(text.split())


def test_cli_renders_a_table(deals_page):
    result = _run(["--limit", "3"], deals_page)
    assert result.exit_code == 0, result.output
    assert "Today's Deals" in _flat(result.output)
    assert FIRST_ASIN in result.output


def test_cli_table_title_counts_shown_against_found(deals_page):
    result = _run(["--limit", "3"], deals_page)
    assert "(3 of 38)" in _flat(result.output)


def test_cli_json_is_valid_and_carries_price_paise(deals_page):
    result = _run(["--json", "--limit", "5"], deals_page)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 5
    assert payload[0]["asin"] == FIRST_ASIN
    assert payload[0]["price_paise"] == FIRST_PRICE_PAISE
    assert payload[0]["price"] == 22
    assert payload[0]["mrp_paise"] == FIRST_MRP_PAISE
    assert payload[0]["discount_percent"] == 89


def test_cli_json_is_ordered_by_discount_descending(deals_page):
    payload = json.loads(_run(["--json"], deals_page).output)
    percents = [row["discount_percent"] for row in payload]
    assert percents == sorted(percents, reverse=True)


def test_cli_json_keeps_rank_as_amazons_page_order_not_the_display_order(deals_page):
    """`rank` is where Amazon put the card; the table's "#" column is our order."""
    payload = json.loads(_run(["--json", "--limit", "38"], deals_page).output)
    ranks = [row["rank"] for row in payload]
    assert sorted(ranks) == list(range(1, 39))
    assert ranks != sorted(ranks), "a discount sort must reorder the page ranks"
    assert payload[0]["rank"] == 1  # the top deal happens to also be card #1


def test_cli_json_of_an_empty_result_is_an_empty_array(deals_page):
    result = _run(["--json", "--min-discount", "95"], deals_page)
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_cli_csv_round_trips_with_a_consistent_header(deals_page):
    result = _run(["--csv", "--limit", "10"], deals_page)
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    header, body = rows[0], rows[1:]
    assert header[0] == "asin"
    assert "price_paise" in header
    assert len(body) == 10
    assert all(len(row) == len(header) for row in body)
    record = dict(zip(header, body[0]))
    assert record["asin"] == FIRST_ASIN
    assert record["price_paise"] == str(FIRST_PRICE_PAISE)


def test_cli_csv_preserves_titles_containing_commas(deals_page):
    result = _run(["--csv"], deals_page)
    rows = list(csv.reader(io.StringIO(result.output)))
    header, body = rows[0], rows[1:]
    titles = [dict(zip(header, row))["title"] for row in body]
    assert any("," in title for title in titles)
    assert all(len(row) == len(header) for row in body)


def test_cli_csv_survives_a_title_with_quotes_and_commas(deals_page):
    nasty = 'Sony "WH-1000XM5", Wireless, 30h Battery'
    page = deals_page.replace(
        "Trending Trunks Zigzag Wave Metal Hairband Zig Zag Hair Band for Men and Women, Black",
        nasty,
    )
    result = _run(["--csv", "--limit", "1"], page)
    rows = list(csv.reader(io.StringIO(result.output)))
    assert len(rows) == 2
    assert len(rows[1]) == len(rows[0])
    assert dict(zip(rows[0], rows[1]))["title"] == nasty


def test_cli_plain_is_tab_separated_with_a_header(deals_page):
    result = _run(["--plain", "--limit", "4"], deals_page)
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert lines[0].split("\t")[:3] == ["asin", "title", "price"]
    assert len(lines) == 5
    assert all(len(line.split("\t")) == 7 for line in lines)


def test_cli_explains_an_empty_result_instead_of_printing_a_blank_table(deals_page):
    result = _run(["--min-discount", "95"], deals_page)
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "No deals at 95% or more" in flat
    assert "Best right now is 89% across 38 deals" in flat


def test_the_empty_message_reports_the_discount_the_filter_actually_used(deals_page):
    """Regression: the message read `discount_percent`, the filter read `deal_discount`.

    On a page whose prices we cannot read, the badge is all we have. The filter
    already knew these were 89% off; the message used to answer "best right now
    is 0%", contradicting it.
    """
    drifted = _mutate(deals_page, _drop("span.dcl-product-price-new"), _unmark_strikes)
    result = _run(["--min-discount", "95"], drifted)
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "Best right now is 89%" in flat
    assert "is 0%" not in flat


def test_cli_explains_a_page_that_parsed_to_nothing():
    result = _run([], "<html><body>client rendered</body></html>")
    assert result.exit_code == 0
    assert "No deals found" in _flat(result.output)


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "-1"]])
def test_cli_rejects_a_non_positive_limit(deals_page, args):
    result = _run(args, deals_page)
    assert result.exit_code == 2


@pytest.mark.parametrize("value", ["-1", "101", "abc"])
def test_cli_rejects_an_out_of_range_min_discount(deals_page, value):
    result = _run(["--min-discount", value], deals_page)
    assert result.exit_code == 2


def test_cli_exits_five_on_a_bot_check(botcheck_page):
    result = _run([], botcheck_page)
    assert result.exit_code == 5
    assert "bot check" in _flat(result.stderr).lower()


def test_cli_exits_four_when_the_deals_page_is_gone():
    result = _run([], status=404)
    assert result.exit_code == 4
