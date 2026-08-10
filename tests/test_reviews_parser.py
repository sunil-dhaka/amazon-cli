"""Reviews must actually contain reviews.

Amazon moved review markup off stable `data-hook` attributes onto CSS-module
class names (`_Y3Itd_single-review-title_2aKRE`). The old selectors still
matched the review *containers*, so `amz reviews` kept printing 13 rows with
correct ratings, authors and dates -- and an empty title and body on every one.

That is the failure mode this suite exists to catch: not a crash, not an error,
just quietly returning nothing where content used to be. These tests assert on
content, because asserting on the row count would still pass today.
"""

import pytest

from amazon_cli.client.parser import parse_reviews_page

from conftest import PRODUCT_ASINS, load_product

#: The build-hash prefixes churn on every Amazon deploy, so nothing may depend
#: on them. Only the stable middle of the class name is matched.
VOLATILE_HASH_FRAGMENTS = ("_Y3Itd_", "_2aKRE", "_325WM")


@pytest.fixture(scope="module")
def reviews_by_asin():
    return {asin: parse_reviews_page(load_product(asin)) for asin in PRODUCT_ASINS}


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_every_product_page_yields_reviews(asin, reviews_by_asin):
    assert len(reviews_by_asin[asin]) > 0


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_every_review_has_a_title(asin, reviews_by_asin):
    reviews = reviews_by_asin[asin]
    titled = [r for r in reviews if r.title.strip()]
    assert len(titled) == len(reviews), (
        f"{len(reviews) - len(titled)} of {len(reviews)} reviews had no title"
    )


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_every_review_has_a_body(asin, reviews_by_asin):
    reviews = reviews_by_asin[asin]
    bodied = [r for r in reviews if r.body.strip()]
    assert len(bodied) == len(reviews)


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_bodies_carry_no_accessibility_or_expander_chrome(asin, reviews_by_asin):
    """The container also holds a11y copy identical on every review.

    Left in, it made every body open with the same 100 characters of
    boilerplate -- present, plausible, and not the review.
    """
    for review in reviews_by_asin[asin]:
        lowered = review.body.lower()
        assert "double tap to read" not in lowered
        assert "brief content visible" not in lowered
        assert "full content visible" not in lowered
        assert not lowered.endswith("read more")
        assert not lowered.endswith("read less")


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_the_star_count_is_not_swallowed_into_the_body(asin, reviews_by_asin):
    # "(5 stars)" sits inside the text container as a screen-reader aside.
    for review in reviews_by_asin[asin]:
        assert "(5 stars)" not in review.body
        assert "(1 star)" not in review.body


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_ratings_authors_and_dates_still_parse(asin, reviews_by_asin):
    """These never broke; assert them so a selector rewrite cannot lose them."""
    for review in reviews_by_asin[asin]:
        assert 0.0 < review.rating <= 5.0
        assert review.author.strip()
        assert review.date.strip()


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_title_and_body_are_read_from_different_nodes(asin, reviews_by_asin):
    """A fallback returning the container text would equate the two everywhere.

    Not "never equal": people genuinely write "Good" as both the headline and
    the whole review, and three such reviews exist in these captures. The
    invariant that actually detects a bad fallback is that the *majority* differ.
    """
    reviews = reviews_by_asin[asin]
    identical = sum(1 for r in reviews if r.title == r.body)
    assert identical < len(reviews) / 2


def test_a_title_is_a_headline_not_a_paragraph(reviews_by_asin):
    for reviews in reviews_by_asin.values():
        for review in reviews:
            assert len(review.title) < 200


def test_bodies_are_substantial(reviews_by_asin):
    """Guards against a selector that matches an empty wrapper."""
    for asin, reviews in reviews_by_asin.items():
        longest = max(len(r.body) for r in reviews)
        assert longest > 80, f"{asin}: longest review body was only {longest} chars"


def test_the_selectors_do_not_depend_on_amazons_build_hashes():
    """Selectors must survive Amazon's next deploy.

    The class names carry per-build hashes (`_Y3Itd_single-review-title_2aKRE`);
    matching one would make the parser break on a schedule Amazon controls.
    Checked against the selector constants, not the file text -- the comments
    quote the hashes deliberately, to explain why they must not be matched.
    """
    from amazon_cli.client import parser

    selectors = parser._REVIEW_TITLE_SELECTORS + parser._REVIEW_BODY_SELECTORS
    assert selectors, "review selectors are missing"
    for selector in selectors:
        for fragment in VOLATILE_HASH_FRAGMENTS:
            assert fragment not in selector, (
                f"selector {selector!r} hard-codes the volatile hash {fragment!r}"
            )


@pytest.mark.parametrize(
    "html", ["", "   ", "not html", "<html><body>no reviews here</body></html>"]
)
def test_junk_input_yields_no_reviews_rather_than_raising(html):
    assert parse_reviews_page(html) == []


def test_the_legacy_data_hook_layout_still_parses():
    """Amazon serves both layouts; neither may regress the other."""
    html = """
    <div data-hook="review">
      <span class="a-profile-name">Asha</span>
      <i data-hook="review-star-rating"><span class="a-icon-alt">4.0 out of 5 stars</span></i>
      <a data-hook="review-title"><span>out of nowhere</span><span>Solid buy</span></a>
      <span data-hook="review-date">Reviewed in India on 3 March 2025</span>
      <span data-hook="review-body">Works exactly as described.Read more</span>
      <span data-hook="avp-badge">Verified Purchase</span>
    </div>
    """
    reviews = parse_reviews_page(html)
    assert len(reviews) == 1
    review = reviews[0]
    assert review.title == "Solid buy"          # not the "out of 5" span
    assert review.body == "Works exactly as described."
    assert review.rating == 4.0
    assert review.author == "Asha"
    assert review.date == "3 March 2025"
    assert review.verified is True
