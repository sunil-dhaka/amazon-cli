"""Shared pytest fixtures.

Every parser test in this suite runs against **real Amazon.in HTML** captured
into `tests/fixtures/`, not hand-written snippets. Hand-written HTML tests the
test author's idea of the markup; captured HTML tests the markup Amazon
actually ships, which is the only thing that can break in production.
"""

import gzip
import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Product pages with independently verified expected values.
PRODUCT_ASINS = [
    "B0BZP2H373",  # Sony WH-1000XM5 -- discount, high review count
    "B0GR177QCS",  # MacBook Air M5 -- lakh-scale price
    "1847941834",  # Atomic Habits -- ISBN-style numeric ASIN, low price
    "B0C3ZYFZ77",  # boAt Airdopes -- 77% discount
    "B0DSFQZTVW",  # Dell laptop
    "B0F7X538TC",  # Sony BRAVIA -- "Only 1 left in stock."
    "B0FDR7FM75",  # Prestige cooker
    "B0DBVVW9XF",  # Nike shoes -- no MRP
]


def load_fixture(name: str) -> str:
    """Read a gzipped HTML fixture by bare name (no extension)."""
    path = FIXTURE_DIR / f"{name}.html.gz"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing fixture {path}. Fixtures are captured, not generated -- "
            f"see tests/fixtures/README.md."
        )
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def load_product(asin: str) -> str:
    """Read the captured product page for an ASIN."""
    return load_fixture(f"product_{asin}")


@pytest.fixture(scope="session")
def product_expected() -> dict:
    """Values the proven Python parser produced from the captured pages.

    Prices here are whole **rupees** (the pre-paise format), so tests multiply
    by 100 when comparing against the paise the parser now returns.
    """
    return json.loads((FIXTURE_DIR / "product_expected.json").read_text())


@pytest.fixture(scope="session")
def product_pages() -> dict[str, str]:
    """Every captured product page, keyed by ASIN. Loaded once per session."""
    return {asin: load_product(asin) for asin in PRODUCT_ASINS}


@pytest.fixture(scope="session")
def offers_page() -> str:
    return load_fixture("offers_B0BZP2H373")


@pytest.fixture(scope="session")
def deals_page() -> str:
    return load_fixture("deals")


@pytest.fixture(scope="session")
def bestsellers_page() -> str:
    return load_fixture("bestsellers")


@pytest.fixture(scope="session")
def search_page() -> str:
    return load_fixture("search_headphones")


@pytest.fixture(scope="session")
def botcheck_page() -> str:
    """A real Akamai bot-verification interstitial served by Amazon.in.

    Captured live: Amazon returns this with HTTP 200, which is exactly why
    status codes alone cannot detect it.
    """
    return load_fixture("botcheck_search")


@pytest.fixture
def tmp_cache_dir(tmp_path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d
