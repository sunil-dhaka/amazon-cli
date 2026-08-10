"""`amz bestsellers` -- category resolution, rank integrity, and the CLI surface.

Two captured shapes are covered, and they are genuinely different pages:

``bestsellers.html.gz``
    the all-departments landing page -- six carousels of six, so ranks run
    1..6 *per department*, six times over.
``bestsellers_electronics.html.gz``
    one department -- a single 1..30 grid.

Anything that cannot be reached from healthy markup is produced by mutating the
captured HTML rather than by hand-writing a card, so the test still runs against
the structure Amazon actually ships.
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
    _CATEGORY_ALIASES,
    _ZG_REF,
    BESTSELLER_CATEGORIES,
    BESTSELLERS_PATH,
    bestsellers_path,
    get_bestsellers,
    parse_bestsellers,
    resolve_category,
    sort_bestsellers,
)
from amazon_cli.client.types import Deal
from amazon_cli.errors import BotCheckError, InputError, RateLimitedError

from conftest import load_fixture

BASE = "https://www.amazon.in"
ALL_URL = f"{BASE}{BESTSELLERS_PATH}"
ELECTRONICS_URL = f"{ALL_URL}/electronics"

#: Read out of `bestsellers_electronics.html.gz` by hand: rank badge "#1",
#: `<span class="_cDEzb_p13n-sc-price_3mJ9Z">₹1,799.00</span>`, and the
#: aria-label "4.3 out of 5 stars, 48,629 ratings".
TOP_ELECTRONICS_ASIN = "B0FMDL81GS"
TOP_ELECTRONICS_PAISE = 1799_00

#: And out of `bestsellers.html.gz`: Beauty #1 at ₹369.00.
TOP_ALL_ASIN = "B09S6M7JQJ"
TOP_ALL_PAISE = 369_00

DEPARTMENTS_ON_THE_LANDING_PAGE = {
    "Beauty",
    "Garden & Outdoors",
    "Home & Kitchen",
    "Jewellery",
    "Shoes & Handbags",
    "Sports, Fitness & Outdoors",
}


# --------------------------------------------------------------------- helpers

def _mutate(html: str, *edits) -> str:
    tree = HTMLParser(html)
    for edit in edits:
        edit(tree)
    return tree.html


def _drop(selector: str):
    def edit(tree):
        for node in tree.css(selector):
            node.decompose()
    return edit


def _break_zg_refs(tree):
    """Amazon renames its `zg_bs_..._d_sccl_<rank>` ref -- the second rank source."""
    for link in tree.css("a"):
        href = link.attributes.get("href")
        if href:
            link.attrs["href"] = href.replace("_d_sccl_", "_d_XXXX_")


def _paise_from_rupees(text: str) -> int:
    """Independent of money.py on purpose -- see tests/test_deals.py."""
    digits = text.replace("₹", "").replace("Rs.", "").replace(",", "").strip()
    whole, _, frac = digits.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


@pytest.fixture(scope="module")
def electronics_page() -> str:
    return load_fixture("bestsellers_electronics")


@pytest.fixture(scope="module")
def electronics(electronics_page):
    return parse_bestsellers(electronics_page)


@pytest.fixture(scope="module")
def all_departments(bestsellers_page):
    return parse_bestsellers(bestsellers_page)


# ------------------------------------------------------------ category resolution

@pytest.mark.parametrize("slug", sorted(BESTSELLER_CATEGORIES))
def test_every_known_slug_resolves_to_itself(slug):
    assert resolve_category(slug) == slug


@pytest.mark.parametrize("slug", sorted(BESTSELLER_CATEGORIES))
def test_every_known_slug_builds_its_own_url(slug):
    assert bestsellers_path(slug) == f"{BESTSELLERS_PATH}/{slug}"


@pytest.mark.parametrize("alias,target", sorted(_CATEGORY_ALIASES.items()))
def test_every_alias_resolves_to_a_real_department(alias, target):
    assert resolve_category(alias) == target
    assert target in BESTSELLER_CATEGORIES
    assert bestsellers_path(alias) == f"{BESTSELLERS_PATH}/{target}"


def test_no_alias_shadows_a_real_slug():
    """Aliases are consulted *before* the table, so a collision would hijack it."""
    assert not set(_CATEGORY_ALIASES) & set(BESTSELLER_CATEGORIES)


def test_none_means_all_departments():
    assert resolve_category(None) is None
    assert bestsellers_path(None) == BESTSELLERS_PATH
    assert bestsellers_path() == BESTSELLERS_PATH


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_a_blank_category_means_all_departments(blank):
    assert resolve_category(blank) is None
    assert bestsellers_path(blank) == BESTSELLERS_PATH


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("ELECTRONICS", "electronics"),
        ("  Electronics  ", "electronics"),
        ("Video Games", "videogames"),
        ("video_games", "videogames"),
        ("video-games", "videogames"),
        ("Home Improvement", "home-improvement"),
        ("home_improvement", "home-improvement"),
        ("Jewellery", "jewelry"),
        ("HOME", "kitchen"),
        ("Pet Supplies", "pet-supplies"),
    ],
)
def test_user_spellings_are_normalised(typed, expected):
    assert resolve_category(typed) == expected


@pytest.mark.parametrize("unknown", ["nonsense", "electronic5", "phones", "kitchn", "book"])
def test_an_unknown_slug_is_an_input_error(unknown):
    with pytest.raises(InputError) as excinfo:
        resolve_category(unknown)
    assert excinfo.value.exit_code == 2
    assert excinfo.value.retryable is False
    assert repr(unknown) in str(excinfo.value)
    assert "--list-categories" in str(excinfo.value)


@pytest.mark.parametrize(
    "typo,suggestion",
    [
        ("electronic", "electronics"),
        ("kitchn", "kitchen"),
        ("book", "books"),
        ("toy", "toys"),
        ("watch", "watches"),
        ("beuty", "beauty"),
    ],
)
def test_a_near_miss_suggests_the_right_department(typo, suggestion):
    with pytest.raises(InputError) as excinfo:
        resolve_category(typo)
    message = str(excinfo.value)
    assert "Did you mean" in message
    assert suggestion in message


def test_a_slug_with_no_near_match_still_points_at_the_category_list():
    with pytest.raises(InputError) as excinfo:
        resolve_category("zzzzzzzzzz")
    assert "Did you mean" not in str(excinfo.value)
    assert "--list-categories" in str(excinfo.value)


def test_an_unknown_slug_never_becomes_a_url():
    with pytest.raises(InputError):
        bestsellers_path("definitely-not-a-department")


def test_every_slug_amazon_itself_links_to_is_in_the_table(bestsellers_page, electronics_page):
    """The table is only useful if it matches the slugs the live page emits."""
    seen = set()
    for html in (bestsellers_page, electronics_page):
        seen |= {m.group(1) for m in _ZG_REF.finditer(html)}
    assert seen
    assert seen <= set(BESTSELLER_CATEGORIES), sorted(seen - set(BESTSELLER_CATEGORIES))


# ------------------------------------------------------ what the electronics page is

def test_the_electronics_page_yields_thirty_ranked_items(electronics):
    assert len(electronics) == 30


def test_the_electronics_number_one_is_the_nord_buds(electronics):
    top = electronics[0]
    assert top.rank == 1
    assert top.asin == TOP_ELECTRONICS_ASIN
    assert top.price == TOP_ELECTRONICS_PAISE
    assert top.rating == 4.3
    assert top.review_count == 48_629
    assert top.badge == "Electronics"
    assert top.title.startswith("OnePlus Nord Buds 3r")


def test_a_single_department_is_ranked_one_through_n_with_no_gaps(electronics):
    ranks = [item.rank for item in electronics]
    assert ranks == list(range(1, 31))
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_every_electronics_asin_is_a_valid_unique_asin(electronics):
    for item in electronics:
        assert len(item.asin) == 10
        assert item.asin.isalnum()
        assert item.asin.upper() == item.asin
    assert len({item.asin for item in electronics}) == len(electronics)


def test_every_electronics_item_belongs_to_the_electronics_department(electronics):
    assert {item.badge for item in electronics} == {"Electronics"}


def test_electronics_ratings_are_plausible(electronics):
    for item in electronics:
        assert 0.0 <= item.rating <= 5.0
        assert item.review_count >= 0
        if item.review_count:
            assert item.rating > 0


def test_prices_match_the_rupee_string_on_the_card(electronics_page, electronics):
    """Cross-check paise against each card's own price text."""
    by_asin = {}
    for node in HTMLParser(electronics_page).css("div[data-asin]"):
        asin = (node.attributes.get("data-asin") or "").strip()
        if not asin:
            continue
        price_node = node.css_first("span[class*='p13n-sc-price']")
        by_asin[asin] = price_node.text(strip=True) if price_node is not None else ""

    for item in electronics:
        expected = _paise_from_rupees(by_asin[item.asin]) if by_asin[item.asin] else money.UNKNOWN
        assert item.price == expected, item.asin


def test_a_card_amazon_prices_only_through_variations_reports_no_price(electronics):
    """Three cards genuinely carry no price. Zero is the honest answer."""
    priceless = {item.asin for item in electronics if item.price == money.UNKNOWN}
    assert priceless == {"B0FQFYXCC4", "B0GL8H6Q22", "B0DGJHBX5Y"}
    assert all(item.title for item in electronics if item.asin in priceless)


def test_no_bestseller_price_is_absurd(electronics, all_departments):
    for item in [*electronics, *all_departments]:
        assert 0 <= item.price <= money.MAX_PAISE


def test_a_bestseller_card_carries_no_mrp(electronics):
    """/gp/bestsellers prints one price; inventing an M.R.P. would be fiction."""
    assert all(item.mrp == 0 and item.discount == "" for item in electronics)


# ----------------------------------------------- what the all-departments page is

def test_the_landing_page_yields_six_departments_of_six(all_departments):
    assert len(all_departments) == 36
    assert {item.badge for item in all_departments} == DEPARTMENTS_ON_THE_LANDING_PAGE


def test_the_landing_page_number_one_is_the_beauty_number_one(all_departments):
    top = all_departments[0]
    assert top.rank == 1
    assert top.asin == TOP_ALL_ASIN
    assert top.price == TOP_ALL_PAISE
    assert top.badge == "Beauty"


def test_landing_page_ranks_run_one_to_six_per_department_not_one_to_thirty_six(all_departments):
    """Not a gap and not a duplicate bug -- Amazon ranks *within* a department.

    Six carousels of six means six items ranked #1. Renumbering them 1..36
    would invent a cross-department ordering Amazon never published.
    """
    assert [item.rank for item in all_departments] == [1, 2, 3, 4, 5, 6] * 6

    per_department: dict[str, list[int]] = {}
    for item in all_departments:
        per_department.setdefault(item.badge, []).append(item.rank)
    assert len(per_department) == 6
    for badge, ranks in per_department.items():
        assert ranks == [1, 2, 3, 4, 5, 6], badge


def test_landing_page_asins_are_valid_and_unique(all_departments):
    for item in all_departments:
        assert len(item.asin) == 10 and item.asin.isalnum()
    assert len({item.asin for item in all_departments}) == len(all_departments)


def test_a_genuine_one_rupee_listing_is_not_treated_as_a_mis_parse(all_departments):
    """"LPG cylinder booking" really is "3 offers from ₹1.00"."""
    lpg = next(i for i in all_departments if i.asin == "B07QP9PTZP")
    assert lpg.price == 100
    assert lpg.title == "LPG cylinder booking"
    assert lpg.badge == "Home & Kitchen"


# ------------------------------------------------------------------ sort and limit

def test_sorting_orders_by_rank_ascending(electronics):
    ordered = sort_bestsellers(electronics)
    assert [item.rank for item in ordered] == list(range(1, 31))


def test_sorting_the_landing_page_interleaves_the_departments(all_departments):
    """Stable sort: every department's #1, then every #2, in Amazon's own order."""
    ordered = sort_bestsellers(all_departments)
    assert [item.rank for item in ordered] == sorted(item.rank for item in all_departments)
    assert [item.badge for item in ordered[:6]] == [
        item.badge for item in all_departments if item.rank == 1
    ]


def test_sorting_does_not_mutate_its_input(all_departments):
    before = [item.asin for item in all_departments]
    sort_bestsellers(all_departments, limit=3)
    assert [item.asin for item in all_departments] == before


@pytest.mark.parametrize("limit,expected", [(1, 1), (10, 10), (30, 30), (31, 30), (500, 30)])
def test_limit_caps_the_list(electronics, limit, expected):
    assert len(sort_bestsellers(electronics, limit=limit)) == expected


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_means_no_cap(electronics, limit):
    """Pinned, not preferred: the CLI blocks this via IntRange(min=1)."""
    assert len(sort_bestsellers(electronics, limit=limit)) == len(electronics)


def test_limit_one_keeps_the_number_one(electronics):
    assert sort_bestsellers(electronics, limit=1)[0].asin == TOP_ELECTRONICS_ASIN


def test_an_unranked_item_sorts_last_and_is_never_renumbered():
    items = [
        Deal(asin="B000000003", title="third", rank=3),
        Deal(asin="B000000000", title="unranked", rank=0),
        Deal(asin="B000000001", title="first", rank=1),
    ]
    ordered = sort_bestsellers(items)
    assert [item.asin for item in ordered] == ["B000000001", "B000000003", "B000000000"]
    assert ordered[-1].rank == 0


def test_sorting_an_empty_list_is_empty():
    assert sort_bestsellers([], limit=5) == []


# ------------------------------------------------------------------ layout drift

def test_a_missing_rank_badge_falls_back_to_the_link_ref(electronics_page):
    """`ref=zg_bs_g_electronics_d_sccl_18` is a second, independent rank source."""
    parsed = parse_bestsellers(_mutate(electronics_page, _drop("span.zg-bdg-text")))
    assert len(parsed) == 30
    assert [item.rank for item in parsed] == list(range(1, 31))
    assert {item.badge for item in parsed} == {"Electronics"}


def test_losing_both_rank_sources_yields_no_rank_rather_than_a_fabricated_one(electronics_page):
    parsed = parse_bestsellers(
        _mutate(electronics_page, _drop("span.zg-bdg-text"), _break_zg_refs)
    )
    assert len(parsed) == 30
    assert {item.rank for item in parsed} == {0}
    assert {item.badge for item in parsed} == {""}
    # And nothing downstream invents one either.
    assert {item.rank for item in sort_bestsellers(parsed)} == {0}


def test_removing_the_ratings_reports_no_rating(electronics_page):
    parsed = parse_bestsellers(_mutate(electronics_page, _drop("div.a-icon-row")))
    assert len(parsed) == 30
    assert {item.rating for item in parsed} == {0.0}
    assert {item.review_count for item in parsed} == {0}
    assert all(item.price or item.title for item in parsed)


def test_removing_every_asin_drops_every_card(electronics_page):
    def strip(tree):
        for node in tree.css("div[data-asin]"):
            del node.attrs["data-asin"]

    assert parse_bestsellers(_mutate(electronics_page, strip)) == []


@pytest.mark.parametrize("asin", ["", "SHORT", "B0FMDL81GS!", "b0fmdl81gs0000"])
def test_a_malformed_asin_attribute_is_skipped(electronics_page, asin):
    def rewrite(tree):
        for node in tree.css("div[data-asin]"):
            node.attrs["data-asin"] = asin

    assert parse_bestsellers(_mutate(electronics_page, rewrite)) == []


def test_removing_the_grid_wrapper_still_finds_the_cards(electronics_page):
    """The parser keys on `div[data-asin]`, not on the grid container above it."""
    def unwrap(tree):
        for node in tree.css("div#gridItemRoot"):
            node.attrs["id"] = "somethingElse"

    assert len(parse_bestsellers(_mutate(electronics_page, unwrap))) == 30


def test_blanking_every_price_keeps_the_ranked_titles(electronics_page):
    parsed = parse_bestsellers(
        _mutate(
            electronics_page,
            _drop("span.a-color-price"),
            _drop("span[class*='p13n-sc-price']"),
            _drop("span.a-price"),
        )
    )
    assert len(parsed) == 30
    assert {item.price for item in parsed} == {money.UNKNOWN}
    assert all(item.title for item in parsed)
    assert [item.rank for item in parsed] == list(range(1, 31))


def test_a_struck_price_is_never_reported_as_the_sale_price(electronics_page):
    """Bestseller cards carry no M.R.P. today; if one appears, it must not win."""
    def strike_the_top_card(tree):
        node = tree.css_first("div[data-asin]")
        for price in node.css("span.a-color-price"):
            price.attrs["class"] = "a-size-base a-color-price a-text-price"
            price.attrs["data-a-strike"] = "true"

    parsed = parse_bestsellers(_mutate(electronics_page, strike_the_top_card))
    top = parsed[0]
    assert top.asin == TOP_ELECTRONICS_ASIN
    assert top.price == money.UNKNOWN
    assert top.price != TOP_ELECTRONICS_PAISE


def test_a_missing_title_node_falls_back_to_the_product_link(electronics_page):
    parsed = parse_bestsellers(
        _mutate(electronics_page, _drop("div.p13n-sc-truncate"), _drop("div[class*='line-clamp']"))
    )
    assert len(parsed) == 30
    assert parsed[0].title.startswith("OnePlus Nord Buds 3r")
    assert parsed[0].price == TOP_ELECTRONICS_PAISE


def test_removing_the_images_never_invents_a_url(electronics_page):
    parsed = parse_bestsellers(_mutate(electronics_page, _drop("img")))
    assert len(parsed) == 30
    assert {item.image_url for item in parsed} == {""}


# ----------------------------------------------------------------- hostile input

@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "\n\t\n",
        "not html at all",
        "<<<>>>&amp;",
        "<html><body><div data-asin=''></div></body></html>",
        "<div data-asin='B0FMDL81GS'></div>",
        "\x00\x01\x02",
        "<html>" + "#1" * 5000 + "</html>",
    ],
)
def test_hostile_input_returns_an_empty_list_and_never_raises(html):
    assert parse_bestsellers(html) == []


@pytest.mark.parametrize("cut", [500, 5_000, 50_000, 200_000])
def test_every_truncation_point_parses_without_raising(electronics_page, cut):
    parsed = parse_bestsellers(electronics_page[:cut])
    assert all(item.rank >= 0 and item.price >= 0 for item in parsed)
    assert all(len(item.asin) == 10 for item in parsed)


def test_the_bot_check_page_raises_rather_than_reporting_no_bestsellers(botcheck_page):
    with pytest.raises(BotCheckError) as excinfo:
        parse_bestsellers(botcheck_page)
    assert excinfo.value.exit_code == 5


# ----------------------------------------------------------------- client wiring

@respx.mock
async def test_no_category_fetches_the_all_departments_page(bestsellers_page):
    route = respx.get(ALL_URL).mock(return_value=httpx.Response(200, text=bestsellers_page))
    async with AmazonClient(max_retries=0) as client:
        items = await get_bestsellers(client)
    assert route.called
    assert len(items) == 36


@respx.mock
async def test_a_category_fetches_that_departments_page(electronics_page):
    route = respx.get(ELECTRONICS_URL).mock(
        return_value=httpx.Response(200, text=electronics_page)
    )
    async with AmazonClient(max_retries=0) as client:
        items = await get_bestsellers(client, "electronics")
    assert route.called
    assert len(items) == 30


@respx.mock
async def test_an_alias_fetches_the_department_it_resolves_to(bestsellers_page):
    route = respx.get(f"{ALL_URL}/kitchen").mock(
        return_value=httpx.Response(200, text=bestsellers_page)
    )
    async with AmazonClient(max_retries=0) as client:
        await get_bestsellers(client, "home")
    assert route.call_count == 1


@respx.mock
async def test_every_known_slug_requests_its_own_url(electronics_page):
    """No slug in the table is a 404 waiting to happen."""
    routes = {
        slug: respx.get(f"{ALL_URL}/{slug}").mock(
            return_value=httpx.Response(200, text=electronics_page)
        )
        for slug in BESTSELLER_CATEGORIES
    }
    async with AmazonClient(max_retries=0) as client:
        for slug in BESTSELLER_CATEGORIES:
            await get_bestsellers(client, slug)
    assert all(route.call_count == 1 for route in routes.values())


@respx.mock
async def test_an_unknown_category_never_reaches_the_network():
    route = respx.get(url__startswith=ALL_URL).mock(return_value=httpx.Response(200, text=""))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(InputError):
            await get_bestsellers(client, "nonsense")
    assert not route.called


@respx.mock
async def test_get_bestsellers_propagates_a_bot_check_instead_of_returning_empty(botcheck_page):
    respx.get(ELECTRONICS_URL).mock(return_value=httpx.Response(200, text=botcheck_page))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(BotCheckError):
            await get_bestsellers(client, "electronics")


@respx.mock
async def test_get_bestsellers_propagates_a_throttle():
    respx.get(ALL_URL).mock(return_value=httpx.Response(503))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(RateLimitedError):
            await get_bestsellers(client)


# -------------------------------------------------------------------------- CLI

def _run(args, page=None, url=ALL_URL, status=200):
    """Invoke the CLI with the bestsellers URL mocked. Never the real network."""
    runner = CliRunner()
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(status, text=page if page is not None else "")
        )
        return runner.invoke(cli, ["--retries", "0", "bestsellers", *args])


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_cli_renders_the_all_departments_table(bestsellers_page):
    result = _run(["--limit", "6"], bestsellers_page)
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Bestsellers -- All departments" in flat
    assert TOP_ALL_ASIN in result.output


def test_cli_renders_a_department_table(electronics_page):
    result = _run(["electronics", "--limit", "5"], electronics_page, url=ELECTRONICS_URL)
    assert result.exit_code == 0, result.output
    assert "Bestsellers -- Electronics" in _flat(result.output)
    assert TOP_ELECTRONICS_ASIN in result.output


def test_cli_labels_an_alias_with_the_department_it_resolved_to(bestsellers_page):
    """Regression: the label echoed what the user typed ("home"), not "Home & Kitchen"."""
    result = _run(["home", "--limit", "3"], bestsellers_page, url=f"{ALL_URL}/kitchen")
    assert result.exit_code == 0, result.output
    assert "Bestsellers -- Home & Kitchen" in _flat(result.output)


def test_cli_list_categories_exits_zero_and_lists_every_slug():
    runner = CliRunner()
    with respx.mock:  # any request at all would raise
        result = runner.invoke(cli, ["bestsellers", "--list-categories"])
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    for slug in BESTSELLER_CATEGORIES:
        assert slug in flat, slug


def test_cli_rejects_an_unknown_category_with_exit_two():
    runner = CliRunner()
    with respx.mock:
        result = runner.invoke(cli, ["bestsellers", "electronic"])
    assert result.exit_code == 2
    flat = _flat(result.stderr)
    assert "Unknown bestsellers category" in flat
    assert "electronics" in flat


def test_cli_json_is_valid_and_carries_price_paise(electronics_page):
    result = _run(["electronics", "--json", "--limit", "5"], electronics_page, url=ELECTRONICS_URL)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 5
    assert [row["rank"] for row in payload] == [1, 2, 3, 4, 5]
    assert payload[0]["asin"] == TOP_ELECTRONICS_ASIN
    assert payload[0]["price_paise"] == TOP_ELECTRONICS_PAISE
    assert payload[0]["price"] == 1799
    assert payload[0]["rating"] == 4.3


def test_cli_json_is_ordered_by_rank(bestsellers_page):
    payload = json.loads(_run(["--json"], bestsellers_page).output)
    ranks = [row["rank"] for row in payload]
    assert ranks == sorted(ranks)


def test_cli_csv_round_trips_with_a_consistent_header(electronics_page):
    result = _run(["electronics", "--csv"], electronics_page, url=ELECTRONICS_URL)
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    header, body = rows[0], rows[1:]
    assert header == ["rank", "asin", "title", "price", "price_paise", "rating",
                      "reviews", "category"]
    assert len(body) == 25  # default --limit
    assert all(len(row) == len(header) for row in body)
    first = dict(zip(header, body[0]))
    assert first["asin"] == TOP_ELECTRONICS_ASIN
    assert first["price_paise"] == str(TOP_ELECTRONICS_PAISE)
    assert first["category"] == "Electronics"


def test_cli_csv_preserves_titles_full_of_commas(electronics_page):
    result = _run(["electronics", "--csv"], electronics_page, url=ELECTRONICS_URL)
    rows = list(csv.reader(io.StringIO(result.output)))
    header, body = rows[0], rows[1:]
    titles = [dict(zip(header, row))["title"] for row in body]
    assert sum("," in title for title in titles) > 5
    assert all(len(row) == len(header) for row in body)


def test_cli_csv_survives_a_title_with_quotes_and_commas(electronics_page):
    nasty = 'OnePlus "Nord Buds 3r", TWS, 54h "playback", Ash Black'

    def rename_the_top_card(tree):
        node = tree.css_first("div[data-asin]").css_first("div[class*='line-clamp']")
        node.replace_with(
            f'<div class="_cDEzb_p13n-sc-css-line-clamp-3_g3dy1">{nasty}</div>'
        )

    page = _mutate(electronics_page, rename_the_top_card)
    result = _run(["electronics", "--csv", "--limit", "1"], page, url=ELECTRONICS_URL)
    rows = list(csv.reader(io.StringIO(result.output)))
    assert len(rows) == 2
    assert len(rows[1]) == len(rows[0])
    assert dict(zip(rows[0], rows[1]))["title"] == nasty


def test_cli_plain_is_tab_separated_with_a_header(electronics_page):
    result = _run(["electronics", "--plain", "--limit", "4"], electronics_page,
                  url=ELECTRONICS_URL)
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert lines[0].split("\t") == ["rank", "asin", "title", "price", "rating", "reviews"]
    assert len(lines) == 5
    assert all(len(line.split("\t")) == 6 for line in lines)


def test_cli_explains_an_empty_result_instead_of_printing_a_blank_table():
    result = _run(["electronics"], "<html><body>nothing here</body></html>",
                  url=ELECTRONICS_URL)
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "No bestsellers parsed for Electronics" in flat
    assert "markup" in flat


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "-3"]])
def test_cli_rejects_a_non_positive_limit(electronics_page, args):
    result = _run(["electronics", *args], electronics_page, url=ELECTRONICS_URL)
    assert result.exit_code == 2


def test_cli_exits_five_on_a_bot_check(botcheck_page):
    result = _run(["electronics"], botcheck_page, url=ELECTRONICS_URL)
    assert result.exit_code == 5
    assert "bot check" in _flat(result.stderr).lower()


def test_cli_exits_four_when_a_department_page_is_gone():
    result = _run(["electronics"], url=ELECTRONICS_URL, status=404)
    assert result.exit_code == 4
