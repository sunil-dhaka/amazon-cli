"""Regression net for `amz variants`, run against real captured pages.

The captured Nike page (`product_variants_B0DBVVW9XF`) carries a twister with
two dimensions, and every other captured product page carries one too -- so the
invariants here are checked against nine real pages, not one.

The invariant that earns its keep is **one selected swatch per dimension**.
Amazon marks the swatch you are currently on in every dimension, and the same
ASIN is what all of those marks point at: the shoe you are looking at is
simultaneously "BLACK" under Colour and "10 UK" under Size. A parser that treats
the ASIN as globally unique silently deletes the current selection from every
dimension after the first.
"""

import asyncio
import csv
import io
import json
import re

import httpx
import pytest
import respx
from click.testing import CliRunner
from selectolax.parser import HTMLParser

from amazon_cli.cli import cli
from amazon_cli.client.types import Variant
from amazon_cli.client.variants import (
    _dimension_of,
    _label_of,
    _price_of,
    group_by_dimension,
    parse_variants,
)
from amazon_cli.commands.variants import CSV_HEADERS, PLAIN_HEADERS, _fetch
from amazon_cli.context import AmzContext
from amazon_cli.errors import BotCheckError, NotFoundError

from conftest import PRODUCT_ASINS, load_fixture, load_product

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

VARIANTS_FIXTURE = "product_variants_B0DBVVW9XF"

#: Every swatch on the captured Nike page, read straight out of the HTML:
#: (dimension, asin, label, selected, available).
NIKE_SWATCHES = [
    ("color_name", "B0D7MMW4VB", "BLACK/HYPER CRIMSON-ASTRONOMY BLUE-SAIL", True, True),
    ("color_name", "B0DP2MW7WB", "BLUE VOID/UNIVERSITY BLUE-WHITE-BLACK", False, True),
    ("color_name", "B0CKZC2XXD", "MIDNIGHT NAVY/PURE PLATINUM-BLACK-WHITE", False, True),
    ("color_name", "B0GKY7N6VY", "ANTHRACITE/WHITE-COOL GREY-BLACK", False, False),
    ("color_name", "B0DJ3R7ZWX", "ASHEN SLATE/METALLIC SILVER-WHITE", False, False),
    ("color_name", "B0CZHDSN8J", "WHITE/FIRE RED-BLACK-PHOTON DUST", False, False),
    ("size_name", "B0D7MPLBNF", "6 UK", False, True),
    ("size_name", "B0D7MMTZ3G", "7 UK", False, True),
    ("size_name", "B0D7MPGBQ9", "8 UK", False, True),
    ("size_name", "B0DJ3Q4CPY", "9 UK", False, False),
    ("size_name", "B0D7MMW4VB", "10 UK", True, True),
    ("size_name", "B0D7MP27ZN", "11 UK", False, True),
]

#: Products captured with no twister at all. `[]` is the answer, not an error.
NO_VARIATION_ASINS = ["1847941834", "B0FDR7FM75"]

#: A two-dimension twister, small enough to serve over a mocked request.
TWISTER_HTML = """<html><body><div id="twister_feature_div">
  <ul>
    <li data-asin="B000000001" data-initiallyselected="true" data-initiallyunavailable="false">
      <span id="color_name_0"><img alt="Jet Black"></span>
    </li>
    <li data-asin="B000000002" data-initiallyselected="false" data-initiallyunavailable="true">
      <span id="color_name_1"><img alt="Dark Cyan"></span>
    </li>
    <li data-asin="B000000001" data-initiallyselected="true" data-initiallyunavailable="false">
      <span id="size_name_0"><span class="swatch-title-text">Large</span></span>
    </li>
  </ul>
</div></body></html>"""

NO_TWISTER_HTML = "<html><body><h1>A single indivisible product</h1></body></html>"


@pytest.fixture(scope="module")
def nike_page() -> str:
    return load_fixture(VARIANTS_FIXTURE)


@pytest.fixture(scope="module")
def nike(nike_page) -> list[Variant]:
    return parse_variants(nike_page)


@pytest.fixture(scope="module")
def every_twister() -> dict[str, list[Variant]]:
    """Variants parsed from every captured page, keyed by ASIN."""
    parsed = {asin: parse_variants(load_product(asin)) for asin in PRODUCT_ASINS}
    parsed[VARIANTS_FIXTURE] = parse_variants(load_fixture(VARIANTS_FIXTURE))
    return parsed


@pytest.fixture
def mock():
    """A respx router bound to amazon.in. Nothing here touches the network."""
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as router:
        yield router


def strip_nodes(html: str, *selectors: str) -> str:
    """The same page with every node matching `selectors` deleted."""
    tree = HTMLParser(html)
    for selector in selectors:
        for node in tree.css(selector):
            node.decompose()
    return tree.html


def drop_attribute(html: str, selector: str, attribute: str) -> str:
    """The same page with `attribute` removed from every matching node."""
    tree = HTMLParser(html)
    for node in tree.css(selector):
        if attribute in node.attributes:
            del node.attrs[attribute]
    return tree.html


# ------------------------------------------------------- the captured Nike page


def test_every_swatch_on_the_captured_page_is_reported(nike):
    """12 swatches, in page order, with the same ASIN under two dimensions."""
    actual = [(v.dimension, v.asin, v.label, v.selected, v.available) for v in nike]
    assert actual == NIKE_SWATCHES


def test_the_captured_page_has_two_dimensions_of_six(nike):
    grouped = group_by_dimension(nike)
    assert {k: len(v) for k, v in grouped.items()} == {"color_name": 6, "size_name": 6}


def test_the_selected_colour_and_the_selected_size_share_one_asin(nike):
    """The regression test for the de-duplication bug.

    B0D7MMW4VB is both "BLACK/HYPER CRIMSON" and "10 UK". Keying uniqueness on
    the ASIN alone dropped the size swatch entirely, so the size list lost its
    current selection and came back one option short.
    """
    shared = [v for v in nike if v.asin == "B0D7MMW4VB"]
    assert [v.dimension for v in shared] == ["color_name", "size_name"]
    assert [v.label for v in shared] == ["BLACK/HYPER CRIMSON-ASTRONOMY BLUE-SAIL", "10 UK"]
    assert all(v.selected for v in shared)


def test_no_price_is_inlined_on_the_captured_swatches(nike):
    """Amazon fetches sibling prices lazily; 0 is the honest answer."""
    assert [v.price for v in nike] == [0] * 12


def test_unavailable_swatches_are_flagged_from_the_negative_attribute(nike):
    unavailable = {v.label for v in nike if not v.available}
    assert unavailable == {
        "ANTHRACITE/WHITE-COOL GREY-BLACK",
        "ASHEN SLATE/METALLIC SILVER-WHITE",
        "WHITE/FIRE RED-BLACK-PHOTON DUST",
        "9 UK",
    }


def test_availability_matches_the_data_attribute_swatch_by_swatch(nike_page, nike):
    scope = HTMLParser(nike_page).css_first("#twister_feature_div")
    expected = [
        node.attributes.get("data-initiallyunavailable") != "true"
        for node in scope.css("li[data-asin]")
    ]
    assert [v.available for v in nike] == expected


# --------------------------------------------- invariants across every fixture


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_every_asin_is_a_well_formed_ten_character_asin(key, every_twister):
    for variant in every_twister[key]:
        assert ASIN_RE.match(variant.asin), variant


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_asins_are_unique_within_a_dimension(key, every_twister):
    for dimension, items in group_by_dimension(every_twister[key]).items():
        asins = [v.asin for v in items]
        assert len(asins) == len(set(asins)), (key, dimension)


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_exactly_one_swatch_is_selected_in_every_dimension(key, every_twister):
    """Holds on all nine captured pages, including the four-dimension MacBook."""
    for dimension, items in group_by_dimension(every_twister[key]).items():
        assert sum(v.selected for v in items) == 1, (key, dimension)


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_every_variant_has_a_label_and_a_dimension(key, every_twister):
    for variant in every_twister[key]:
        assert variant.label
        assert variant.dimension
        assert len(variant.label) <= 80


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_no_label_leaks_amazons_placeholder_copy(key, every_twister):
    """"See available options" is slot-info Amazon paints on unresolved
    swatches; it is not part of the option's name."""
    for variant in every_twister[key]:
        assert "see available options" not in variant.label.lower()


@pytest.mark.parametrize("key", PRODUCT_ASINS + [VARIANTS_FIXTURE])
def test_a_selected_swatch_is_always_available(key, every_twister):
    for variant in every_twister[key]:
        if variant.selected:
            assert variant.available


@pytest.mark.parametrize("asin", NO_VARIATION_ASINS)
def test_a_product_without_variations_yields_an_empty_list(asin, every_twister):
    assert every_twister[asin] == []


def test_the_macbook_keeps_all_four_of_its_dimensions(every_twister):
    """Its own ASIN is the selected swatch in four dimensions at once."""
    grouped = group_by_dimension(every_twister["B0GR177QCS"])
    assert {k: len(v) for k, v in grouped.items()} == {
        "style_name": 2,
        "size_name": 2,
        "color_name": 4,
        "configuration": 2,
    }
    assert sum(v.asin == "B0GR177QCS" for v in every_twister["B0GR177QCS"]) == 4


def test_the_television_reports_all_fifteen_swatches(every_twister):
    grouped = group_by_dimension(every_twister["B0F7X538TC"])
    assert {k: len(v) for k, v in grouped.items()} == {"style_name": 7, "size_name": 8}
    assert "43 inches" in {v.label for v in every_twister["B0F7X538TC"]}


# ------------------------------------------------------------------ layout drift


def test_deleting_the_twister_yields_no_variants(nike_page):
    mutated = strip_nodes(nike_page, "#twister_feature_div", "#twister")
    assert parse_variants(mutated) == []


def test_deleting_the_data_asin_attributes_yields_no_variants(nike_page):
    mutated = drop_attribute(nike_page, "li[data-asin]", "data-asin")
    assert parse_variants(mutated) == []


def test_a_swatch_with_a_malformed_asin_is_dropped_not_reported():
    html = """<html><body><div id="twister_feature_div">
      <li data-asin="TOO-SHORT"><span id="color_name_0"><img alt="Red"></span></li>
      <li data-asin=""><span id="color_name_1"><img alt="Blue"></span></li>
      <li data-asin="B000000001"><span id="color_name_2"><img alt="Green"></span></li>
    </div></body></html>"""
    assert [v.asin for v in parse_variants(html)] == ["B000000001"]


def test_deleting_the_dimension_spans_leaves_variants_without_a_dimension(nike_page):
    mutated = strip_nodes(nike_page, "#twister_feature_div span[id]")
    found = parse_variants(mutated)
    assert found  # the swatches are still there
    assert {v.dimension for v in found} == {""}


def test_variants_without_a_dimension_land_in_one_option_bucket(nike_page):
    mutated = strip_nodes(nike_page, "#twister_feature_div span[id]")
    grouped = group_by_dimension(parse_variants(mutated))
    assert list(grouped) == ["option"]


def test_deleting_the_labels_still_reports_the_asins(nike_page):
    mutated = strip_nodes(nike_page, "#twister_feature_div img")
    found = parse_variants(mutated)
    assert len(found) == 12
    assert all(ASIN_RE.match(v.asin) for v in found)


def test_deleting_the_selected_flags_reports_nothing_as_selected(nike_page):
    mutated = drop_attribute(nike_page, "li[data-asin]", "data-initiallyselected")
    found = parse_variants(mutated)
    assert len(found) == 12
    assert not any(v.selected for v in found)


def test_deleting_the_unavailable_flags_reads_everything_as_available(nike_page):
    """Amazon spells only the negative, so absence must mean available."""
    mutated = drop_attribute(nike_page, "li[data-asin]", "data-initiallyunavailable")
    assert all(v.available for v in parse_variants(mutated))


def test_swatches_outside_the_twister_are_ignored_when_a_twister_exists(nike_page):
    """A "similar products" carousel must not become a variation list."""
    intruder = '<li data-asin="B0INTRUDER"><img alt="Sponsored"></li>'
    mutated = nike_page.replace("</body>", intruder + "</body>")
    assert "B0INTRUDER" not in {v.asin for v in parse_variants(mutated)}


def test_a_page_with_swatches_but_no_twister_container_still_parses():
    html = """<html><body><ul>
      <li data-asin="B000000001"><span id="color_name_0"><img alt="Red"></span></li>
    </ul></body></html>"""
    assert [v.label for v in parse_variants(html)] == ["Red"]


# --------------------------------------------------------------- hostile input

HOSTILE = {
    "empty": "",
    "whitespace": "   \n\t  ",
    "plain text": "no markup here",
    "unclosed tag soup": "<li data-asin=",
    "deeply nested junk": "<div>" * 500 + '<li data-asin="B000000001">x' + "</div>" * 500,
    "json": '{"variants": []}',
    "null byte": "\x00\x00\x00",
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_input_never_raises(case):
    assert isinstance(parse_variants(HOSTILE[case]), list)


def test_none_is_treated_as_an_empty_page():
    assert parse_variants(None) == []


@pytest.mark.parametrize("cut", [500, 5_000, 50_000, 200_000])
def test_a_truncated_page_never_raises_and_never_invents_an_asin(cut, nike_page):
    for variant in parse_variants(nike_page[:cut]):
        assert ASIN_RE.match(variant.asin)


def test_the_bot_check_page_parses_to_nothing(botcheck_page):
    """Empty, not wrong. The client layer is what must raise -- see
    `test_a_bot_check_propagates_out_of_the_fetch`."""
    assert parse_variants(botcheck_page) == []


# ------------------------------------------------------------- field extraction


def test_an_inlined_price_is_read_as_paise():
    node = HTMLParser(
        '<li data-asin="B000000001">'
        '<span class="a-price"><span class="a-offscreen">&#8377;1,299.50</span></span></li>'
    ).css_first("li")
    assert _price_of(node) == 129950


@pytest.mark.parametrize(
    "markup, expected",
    [
        ('<span class="a-color-price">&#8377;999</span>', 99900),
        ('<span class="a-price-whole">2,499</span>', 249900),
        ("<span>no price at all</span>", 0),
        ('<span class="a-color-price">-26%</span>', 0),
    ],
)
def test_price_extraction_handles_the_markup_amazon_actually_uses(markup, expected):
    node = HTMLParser(f'<li data-asin="B000000001">{markup}</li>').css_first("li")
    assert _price_of(node) == expected


def test_the_dimension_index_is_stripped_off_the_slot_id():
    node = HTMLParser('<li><span id="color_name_12"></span></li>').css_first("li")
    assert _dimension_of(node) == "color_name"


def test_an_announce_suffix_is_stripped_off_the_slot_id():
    node = HTMLParser('<li><span id="size_name_3-announce"></span></li>').css_first("li")
    assert _dimension_of(node) == "size_name"


def test_an_unrecognised_id_is_not_mistaken_for_a_dimension():
    node = HTMLParser('<li><span id="dimension-slot-info-0"></span></li>').css_first("li")
    assert _dimension_of(node) == ""


def test_the_image_alt_is_preferred_as_a_label():
    node = HTMLParser('<li><img alt="Jet Black"><span>ignored</span></li>').css_first("li")
    assert _label_of(node) == "Jet Black"


def test_a_placeholder_alt_is_not_used_as_a_label():
    node = HTMLParser(
        '<li><img alt="See available options">'
        '<span class="swatch-title-text">43 inches</span></li>'
    ).css_first("li")
    assert _label_of(node) == "43 inches"


def test_the_swatch_title_wins_over_the_announce_text_that_swallows_slot_info():
    """The announce span concatenates the title with the slot-info placeholder,
    which used to surface as the label "43 inchesSee available options"."""
    node = HTMLParser(
        '<li><span id="size_name_0-announce">'
        '<span class="swatch-title-text">43 inches</span>'
        '<span class="default-slot-unavailable">See available options</span>'
        "</span></li>"
    ).css_first("li")
    assert _label_of(node) == "43 inches"


def test_the_announce_text_is_still_used_when_there_is_no_title_node():
    node = HTMLParser('<li><span id="style_name_0-announce">BRAVIA 9</span></li>').css_first("li")
    assert _label_of(node) == "BRAVIA 9"


def test_a_swatch_with_nothing_to_say_gets_an_empty_label():
    node = HTMLParser("<li><span>-</span></li>").css_first("li")
    assert _label_of(node) == ""


# ------------------------------------------------------------------- grouping


def test_grouping_preserves_page_order_within_a_dimension(nike):
    sizes = group_by_dimension(nike)["size_name"]
    assert [v.label for v in sizes] == ["6 UK", "7 UK", "8 UK", "9 UK", "10 UK", "11 UK"]


def test_grouping_preserves_first_appearance_order_of_the_dimensions(nike):
    assert list(group_by_dimension(nike)) == ["color_name", "size_name"]


def test_grouping_an_empty_list_is_an_empty_mapping():
    assert group_by_dimension([]) == {}


def test_grouping_does_not_drop_anything():
    variants = [Variant(asin=f"B00000000{i}", label=str(i), dimension="size_name") for i in range(5)]
    grouped = group_by_dimension(variants)
    assert sum(len(v) for v in grouped.values()) == len(variants)


# --------------------------------------------------------------- network layer


def fetch(asin: str) -> list[Variant]:
    return asyncio.run(_fetch(AmzContext(retries=0), asin))


def test_the_product_page_is_what_gets_fetched(mock):
    route = mock.get("/dp/B0DBVVW9XF").mock(
        return_value=httpx.Response(200, text=TWISTER_HTML)
    )
    found = fetch("B0DBVVW9XF")
    assert route.call_count == 1
    assert [v.asin for v in found] == ["B000000001", "B000000002", "B000000001"]


def test_a_lowercase_asin_is_normalised_into_the_path(mock):
    route = mock.get("/dp/B0DBVVW9XF").mock(return_value=httpx.Response(200, text=TWISTER_HTML))
    fetch("  b0dbvvw9xf  ")
    assert route.call_count == 1


def test_a_malformed_asin_never_reaches_the_network(mock):
    with pytest.raises(ValueError):
        fetch("nope")
    assert mock.calls.call_count == 0


def test_a_404_surfaces_as_not_found(mock):
    mock.get("/dp/B0DBVVW9XF").mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError):
        fetch("B0DBVVW9XF")


def test_a_bot_check_propagates_out_of_the_fetch(mock):
    """A captcha must never be laundered into "this product has no variations"."""
    mock.get("/dp/B0DBVVW9XF").mock(
        return_value=httpx.Response(200, text="<html>Enter the characters you see below</html>")
    )
    with pytest.raises(BotCheckError) as excinfo:
        fetch("B0DBVVW9XF")
    assert excinfo.value.exit_code == 5


def test_a_real_captured_page_survives_a_round_trip_through_the_client(mock, nike_page):
    """The 1 MB capture must not trip the bot-check heuristic on its way in."""
    mock.get("/dp/B0DBVVW9XF").mock(return_value=httpx.Response(200, text=nike_page))
    assert len(fetch("B0DBVVW9XF")) == 12


# ------------------------------------------------------------------------- CLI


def run_cli(args, html=TWISTER_HTML, asin="B0DBVVW9XF", status=200):
    """Invoke `amz` through the group, with the network mocked out."""
    with respx.mock(base_url="https://www.amazon.in", assert_all_called=False) as mock:
        mock.get(f"/dp/{asin}").mock(return_value=httpx.Response(status, text=html))
        return CliRunner().invoke(cli, args)


def test_cli_renders_a_table_per_dimension():
    result = run_cli(["variants", "B0DBVVW9XF"])
    assert result.exit_code == 0, result.output
    assert "3 variations across 2 dimensions" in result.output
    assert "Colour (2)" in result.output
    assert "Size (1)" in result.output


def test_cli_json_is_valid_and_carries_paise():
    result = run_cli(["variants", "B0DBVVW9XF", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["asin"] for row in payload] == ["B000000001", "B000000002", "B000000001"]
    assert all(row["price_paise"] == 0 for row in payload)
    assert [row["dimension"] for row in payload] == ["color_name", "color_name", "size_name"]


def test_cli_json_reports_the_selection_in_each_dimension():
    payload = json.loads(run_cli(["variants", "B0DBVVW9XF", "--json"]).output)
    selected = [row for row in payload if row["selected"]]
    assert {row["dimension"] for row in selected} == {"color_name", "size_name"}


def test_cli_csv_round_trips_with_a_matching_header_width():
    result = run_cli(["variants", "B0DBVVW9XF", "--csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    assert rows[0] == CSV_HEADERS
    assert len(rows) == 4
    assert all(len(row) == len(CSV_HEADERS) for row in rows)
    assert dict(zip(rows[0], rows[1]))["asin"] == "B000000001"


def test_cli_csv_quotes_a_label_containing_a_comma():
    html = TWISTER_HTML.replace("Jet Black", "Black, Matte")
    rows = list(csv.reader(io.StringIO(run_cli(["variants", "B0DBVVW9XF", "--csv"], html=html).output)))
    assert len(rows) == 4
    assert all(len(row) == len(CSV_HEADERS) for row in rows)
    assert dict(zip(rows[0], rows[1]))["label"] == "Black, Matte"


def test_cli_plain_is_tab_separated_with_the_documented_headers():
    lines = run_cli(["variants", "B0DBVVW9XF", "--plain"]).output.splitlines()
    assert lines[0].split("\t") == PLAIN_HEADERS
    assert len(lines) == 4
    assert all(len(line.split("\t")) == len(PLAIN_HEADERS) for line in lines[1:])


def test_cli_explains_a_product_with_no_variations_and_exits_zero():
    result = run_cli(["variants", "B0DBVVW9XF"], html=NO_TWISTER_HTML)
    assert result.exit_code == 0
    assert "no selectable variations" in result.output


def test_cli_json_of_a_product_with_no_variations_is_an_empty_array():
    result = run_cli(["variants", "B0DBVVW9XF", "--json"], html=NO_TWISTER_HTML)
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_cli_csv_of_a_product_with_no_variations_is_headers_only():
    result = run_cli(["variants", "B0DBVVW9XF", "--csv"], html=NO_TWISTER_HTML)
    assert result.exit_code == 0
    assert list(csv.reader(io.StringIO(result.output))) == [CSV_HEADERS]


@pytest.mark.parametrize("bad", ["notanasin", "B0DBVVW9X", "B0DBVVW9XFF", "", "B0DBVV-9XF"])
def test_cli_rejects_a_malformed_asin_with_exit_two(bad):
    result = CliRunner().invoke(cli, ["variants", bad])
    assert result.exit_code == 2
    assert "ASIN" in result.output


def test_cli_exit_code_for_a_missing_product_is_four():
    result = run_cli(["variants", "B0DBVVW9XF"], status=404, html="")
    assert result.exit_code == 4


def test_cli_exit_code_for_a_bot_check_is_five():
    result = run_cli(
        ["--retries", "0", "variants", "B0DBVVW9XF"],
        html="<html>Enter the characters you see below</html>",
    )
    assert result.exit_code == 5


def test_cli_renders_the_real_captured_page(nike_page):
    result = run_cli(["variants", "B0DBVVW9XF"], html=nike_page)
    assert result.exit_code == 0, result.output
    assert "12 variations across 2 dimensions" in result.output
    assert "Colour (6)" in result.output
    assert "Size (6)" in result.output
    assert "10 UK" in result.output
