"""Bot-check detection.

Amazon serves its bot-verification interstitial with **HTTP 200**, so status
codes cannot detect it. Before this existed, a throttled `amz search` printed
"0 results" and a throttled `amz product` printed a product with every field
blank -- both silent, both indistinguishable from a genuine empty result.

`tests/fixtures/botcheck_search.html.gz` is a real one, captured live from
Amazon.in while it was rate-limiting this machine.
"""

import httpx
import pytest
import respx

from amazon_cli.client.base import AmazonClient, looks_like_bot_check, validate_asin
from amazon_cli.client.parser import parse_search_results
from amazon_cli.errors import BotCheckError, NetworkError, NotFoundError, RateLimitedError

from conftest import PRODUCT_ASINS, load_fixture, load_product

BASE = "https://www.amazon.in"


def test_the_real_captured_interstitial_is_detected(botcheck_page):
    assert looks_like_bot_check(botcheck_page) is True


def test_the_real_interstitial_would_otherwise_parse_as_zero_results(botcheck_page):
    """Why this matters: without detection, this page is a silent empty answer."""
    products, total = parse_search_results(botcheck_page)
    assert products == []
    assert total == 0


@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_no_healthy_product_page_is_mistaken_for_a_bot_check(asin):
    """A false positive turns a good page into a hard error -- worse than useless."""
    assert looks_like_bot_check(load_product(asin)) is False


@pytest.mark.parametrize(
    "name", ["deals", "bestsellers", "bestsellers_electronics", "search_headphones"]
)
def test_no_healthy_listing_page_is_mistaken_for_a_bot_check(name):
    assert looks_like_bot_check(load_fixture(name)) is False


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>Enter the characters you see below</body></html>",
        "<html><body>Type the characters you see in this image</body></html>",
        "<html><form action='/errors/validateCaptcha'></form></html>",
        "<html>To discuss automated access to Amazon data please contact "
        "api-services-support@amazon.com</html>",
    ],
)
def test_known_captcha_phrasings_are_detected(html):
    assert looks_like_bot_check(html) is True


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "plain text"])
def test_empty_or_trivial_input_is_not_a_bot_check(html):
    assert looks_like_bot_check(html) is False


def test_review_prose_mentioning_a_captcha_is_not_a_bot_check():
    """The markers must be narrow enough to survive customer reviews.

    A loose marker here would classify a healthy page as blocked. This body is
    long, so only the strict markers apply.
    """
    html = (
        "<html><body>" + ("<p>Great product. " * 2000) + "</p>"
        "<p>I had to enter a code to register it.</p>"
        "<p>Looking for something similar? This is it.</p>"
        "</body></html>"
    )
    assert looks_like_bot_check(html) is False


# ------------------------------------------------------- client-level behaviour

@respx.mock
async def test_fetch_raises_botcheck_on_a_200_interstitial(botcheck_page):
    respx.get(f"{BASE}/s").mock(return_value=httpx.Response(200, text=botcheck_page))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(BotCheckError) as excinfo:
            await client.fetch("/s", params={"k": "headphones"})
    # The message has to tell the user what to do about it.
    assert "bot check" in str(excinfo.value).lower()
    assert excinfo.value.retryable is True
    assert excinfo.value.exit_code == 5


@respx.mock
async def test_a_bot_check_is_retried_before_giving_up(botcheck_page):
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(
        return_value=httpx.Response(200, text=botcheck_page)
    )
    async with AmazonClient(max_retries=2) as client:
        with pytest.raises(BotCheckError):
            await client.fetch("/dp/B0BZP2H373")
    assert route.call_count == 3  # initial + 2 retries


@respx.mock
async def test_a_transient_bot_check_recovers_on_retry(botcheck_page):
    good = load_product("B0BZP2H373")
    route = respx.get(f"{BASE}/dp/B0BZP2H373")
    route.side_effect = [
        httpx.Response(200, text=botcheck_page),
        httpx.Response(200, text=good),
    ]
    async with AmazonClient(max_retries=2) as client:
        html = await client.fetch("/dp/B0BZP2H373")
    assert "productTitle" in html
    assert route.call_count == 2


@respx.mock
async def test_404_raises_not_found_and_is_not_retried():
    route = respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    async with AmazonClient(max_retries=3) as client:
        with pytest.raises(NotFoundError) as excinfo:
            await client.fetch("/dp/B0ZZZZZZZZ")
    assert route.call_count == 1, "a missing page must not be retried"
    assert excinfo.value.retryable is False
    assert excinfo.value.exit_code == 4


@respx.mock
async def test_throttling_status_raises_rate_limited_after_retries():
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(return_value=httpx.Response(503))
    async with AmazonClient(max_retries=1) as client:
        with pytest.raises(RateLimitedError):
            await client.fetch("/dp/B0BZP2H373")
    assert route.call_count == 2


@respx.mock
async def test_server_error_becomes_network_error_and_is_retried():
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(return_value=httpx.Response(500))
    async with AmazonClient(max_retries=1) as client:
        with pytest.raises(NetworkError):
            await client.fetch("/dp/B0BZP2H373")
    assert route.call_count == 2


@respx.mock
async def test_a_connection_failure_is_retried_then_surfaces_as_network_error():
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    async with AmazonClient(max_retries=1) as client:
        with pytest.raises(NetworkError):
            await client.fetch("/dp/B0BZP2H373")
    assert route.call_count == 2


@respx.mock
async def test_a_timeout_surfaces_as_network_error_not_an_httpx_exception():
    respx.get(f"{BASE}/dp/B0BZP2H373").mock(side_effect=httpx.ReadTimeout("slow"))
    async with AmazonClient(max_retries=0) as client:
        with pytest.raises(NetworkError):
            await client.fetch("/dp/B0BZP2H373")


@respx.mock
async def test_a_good_page_is_returned_without_retrying():
    good = load_product("B0C3ZYFZ77")
    route = respx.get(f"{BASE}/dp/B0C3ZYFZ77").mock(
        return_value=httpx.Response(200, text=good)
    )
    async with AmazonClient() as client:
        html = await client.fetch("/dp/B0C3ZYFZ77")
    assert html == good
    assert route.call_count == 1


# ------------------------------------------------------------------ ASIN input

@pytest.mark.parametrize("asin", PRODUCT_ASINS)
def test_every_fixture_asin_validates(asin):
    assert validate_asin(asin) == asin


@pytest.mark.parametrize("raw", ["b0bzp2h373", "  b0bzp2h373  "])
def test_asin_validation_normalises(raw):
    assert validate_asin(raw) == "B0BZP2H373"


@pytest.mark.parametrize(
    "raw", ["", "   ", None, "B0BZP2H37", "B0BZP2H3733", "B0!ZP2H373", "B0 ZP2H373"]
)
def test_asin_validation_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        validate_asin(raw)
