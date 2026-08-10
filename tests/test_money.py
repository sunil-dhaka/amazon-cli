"""Money is the part of `amz` that must never be wrong.

Every one of these cases is a bug the previous implementation actually had, or a
neighbour of one. The old parser was
``int(float(re.sub(r"[^\\d.]", "", text)))``, which crashed on a labelled price,
read ``-26%`` as 26, truncated paise, and grouped digits the Western way.
"""

import pytest

from amazon_cli import money


# ---------------------------------------------------------------- parse_paise

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("₹3,325", 332_500),
        ("₹1,72,490.00", 17_249_000),
        ("₹12,34,567.89", 123_456_789),
        ("459.50", 45_950),
        ("₹ 1,234", 123_400),
        (" ₹ 25,990.00 ", 2_599_000),
        ("₹459.5", 45_950),          # single decimal digit is tenths
        ("₹459.", 45_900),           # trailing dot
        ("₹0.01", 1),                # smallest legitimate price
        ("₹1", 100),
    ],
)
def test_parses_real_amazon_price_strings(raw, expected):
    assert money.parse_paise(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("M.R.P.: ₹34,990.00", 3_499_000),
        ("Deal of the Day ₹799", 79_900),
        ("Price: ₹1,72,490.00 (incl. of all taxes)", 17_249_000),
    ],
)
def test_reads_a_price_out_of_a_labelled_string(raw, expected):
    """The label's full stops must not be folded into the number.

    `float('...34990.00')` was an unhandled ValueError -- a hard crash of
    `amz product` triggered purely by Amazon's copy.
    """
    assert money.parse_paise(raw) == expected


def test_more_than_two_decimals_truncate_rather_than_round():
    # Rounding a scraped third digit could tip a price onto a target it never met.
    assert money.parse_paise("₹459.999") == 45_999


def test_two_concatenated_prices_keep_only_the_first():
    assert money.parse_paise("1,299.001,499.00") == 129_900


@pytest.mark.parametrize("raw", ["-26%", "-7%", "26% off", "77%", "26 %"])
def test_rejects_percentages_rather_than_reading_them_as_money(raw):
    """A discount badge must never become a price -- that is a silent wrong answer."""
    assert money.parse_paise(raw) == money.UNKNOWN


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "Currently unavailable", "₹", "--", "N/A"]
)
def test_rejects_input_with_no_usable_number(raw):
    assert money.parse_paise(raw) == money.UNKNOWN


@pytest.mark.parametrize("raw", ["₹0", "0.00", "0"])
def test_rejects_zero(raw):
    assert money.parse_paise(raw) == money.UNKNOWN


def test_accepts_exactly_one_crore_and_rejects_above():
    assert money.parse_paise("10000000") == money.MAX_PAISE
    assert money.parse_paise("10000001") == money.UNKNOWN


@pytest.mark.parametrize("raw", ["9" * 30, "1" + "0" * 25, "9" * 400])
def test_absurd_digit_runs_cannot_overflow_or_hang(raw):
    assert money.parse_paise(raw) == money.UNKNOWN


def test_never_raises_on_hostile_input():
    hostile = [
        "\x00\x01", "₹" * 500, "." * 100, "," * 100, "1" + "," * 50 + "2",
        "1.2.3.4.5", "\n\t ₹1,000 \n", "١٢٣", "１２３", "%1,000",
    ]
    for text in hostile:
        result = money.parse_paise(text)
        assert result == money.UNKNOWN or 1 <= result <= money.MAX_PAISE


# ------------------------------------------------------------- Indian grouping

@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"), (7, "7"), (99, "99"), (999, "999"),
        (1_000, "1,000"), (9_999, "9,999"),
        (10_000, "10,000"), (99_999, "99,999"),
        (100_000, "1,00,000"), (172_490, "1,72,490"),
        (1_234_567, "12,34,567"),
        (10_000_000, "1,00,00,000"),
        (123_456_789, "12,34,56,789"),
    ],
)
def test_groups_digits_the_indian_way(value, expected):
    """1,72,490 -- not 172,490. This is an Amazon.in-only tool."""
    assert money.group_indian(value) == expected


def test_grouping_handles_negatives():
    assert money.group_indian(-1_234_567) == "-12,34,567"
    assert money.group_indian(-99) == "-99"


# ------------------------------------------------------------------ formatting

@pytest.mark.parametrize(
    "paise,expected",
    [
        (17_249_000, "Rs.1,72,490"),
        (332_500, "Rs.3,325"),
        (2_599_000, "Rs.25,990"),
        (45_950, "Rs.459.50"),
        (45_905, "Rs.459.05"),
        (1, "Rs.0.01"),
        (money.UNKNOWN, "--"),
    ],
)
def test_formats_with_indian_grouping_and_optional_paise(paise, expected):
    assert money.format_inr(paise) == expected


def test_format_round_trips_through_parse():
    for paise in [1, 100, 45_950, 332_500, 2_599_000, 17_249_000, 123_456_789]:
        assert money.parse_paise(money.format_inr(paise)) == paise


@pytest.mark.parametrize(
    "paise,expected",
    [
        (45_900, "Rs.459"), (332_500, "Rs.3.3k"), (2_599_000, "Rs.26k"),
        (17_249_000, "Rs.1.7L"), (100_000_000, "Rs.10L"),
        (1_000_000_000, "Rs.1Cr"), (500_000, "Rs.5k"),
        (money.UNKNOWN, "--"),
    ],
)
def test_compact_form_scales_to_thousands_lakhs_crores(paise, expected):
    assert money.format_compact(paise) == expected


def test_rupees_keeps_whole_numbers_as_ints_for_json():
    # Existing scripts read `price` as an int; only fractional prices become floats.
    assert money.rupees(369_500) == 3695
    assert isinstance(money.rupees(369_500), int)
    assert money.rupees(45_950) == 459.5
    assert money.rupees(money.UNKNOWN) == 0


# ----------------------------------------------------------------- percentages

@pytest.mark.parametrize(
    "price,mrp,expected",
    [
        (2_599_000, 3_499_000, 26),    # Sony WH-1000XM5
        (17_249_000, 18_590_000, 7),   # MacBook Air
        (45_900, 89_900, 49),          # Atomic Habits
        (79_900, 349_000, 77),         # boAt
        (6_299_000, 9_947_800, 37),    # Dell
        (16_899_000, 24_990_000, 32),  # BRAVIA
        (797_900, 1_199_900, 34),      # Prestige
    ],
)
def test_discount_percent_matches_what_amazon_displays(price, mrp, expected):
    assert money.discount_percent(price, mrp) == expected


@pytest.mark.parametrize(
    "price,mrp", [(1000, 1000), (1200, 1000), (0, 1000), (1000, 0), (-5, 100)]
)
def test_discount_percent_is_never_negative_or_bogus(price, mrp):
    assert money.discount_percent(price, mrp) == 0


def test_discount_percent_rounds_half_up():
    assert money.discount_percent(19_900, 20_000) == 1   # 0.5% -> 1
    assert money.discount_percent(39_900, 40_000) == 0   # 0.25% -> 0


def test_change_percent_is_signed():
    assert money.change_percent(10_000, 12_000) == 20
    assert money.change_percent(10_000, 8_000) == -20
    assert money.change_percent(10_000, 10_000) == 0
    assert money.change_percent(0, 10_000) == 0


# -------------------------------------------------------------------- sane_mrp

def test_sane_mrp_discards_a_list_price_that_is_not_higher():
    """An MRP at or below the price is a mis-parse, not a discount."""
    assert money.sane_mrp(3_499_000, 2_599_000) == 3_499_000
    assert money.sane_mrp(2_599_000, 2_599_000) == money.UNKNOWN
    assert money.sane_mrp(2_000_000, 2_599_000) == money.UNKNOWN
    assert money.sane_mrp(0, 2_599_000) == money.UNKNOWN


def test_sane_mrp_is_kept_when_there_is_no_price_to_compare():
    # No buy-box price parsed -- we cannot call the MRP wrong, so keep it.
    assert money.sane_mrp(3_499_000, 0) == 3_499_000


def test_every_parsed_price_is_a_positive_whole_number_of_paise():
    samples = [
        "₹1", "₹1.01", "₹99,999", "₹1,00,000", "₹9,99,999.99",
        "₹12,34,567.89", "459", "459.5", "₹ 3,325.00",
    ]
    for text in samples:
        paise = money.parse_paise(text)
        assert isinstance(paise, int)
        assert 0 < paise <= money.MAX_PAISE
