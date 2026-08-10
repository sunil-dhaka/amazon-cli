# Test fixtures

Real Amazon.in pages, captured live and gzipped. Every parser test in this suite
runs against these rather than against hand-written HTML.

That distinction matters. Hand-written HTML tests the test author's *idea* of
Amazon's markup, which is always tidier than the real thing and never drifts. A
captured page tests the markup Amazon actually ships — so when Amazon changes it,
a test fails instead of the scraper silently starting to return nothing, or
worse, the wrong number.

## What is here

| File | What it is | Why it earns its place |
|---|---|---|
| `product_<ASIN>.html.gz` (8) | Product detail pages | The core parser's regression net |
| `product_expected.json` | Independently verified values for those 8 | Prices are whole **rupees** here; the parser returns **paise**, so tests multiply by 100 |
| `offers_<ASIN>.html.gz` (2) | `/gp/offer-listing/<asin>` | Proves only the buy box is server-rendered |
| `product_variants_B0DBVVW9XF.html.gz` | Nike shoes | 6 colours + 5 sizes in the twister |
| `deals.html.gz` | `/deals` | Deal cards |
| `bestsellers.html.gz`, `bestsellers_electronics.html.gz` | `/gp/bestsellers[/<slug>]` | Ranked SSR grids |
| `search_headphones.html.gz`, `search_price_asc.html.gz` | `/s?k=…` | Search results, incl. a sorted variant |
| `botcheck_search.html.gz` | An Akamai bot-verification interstitial | **Amazon served this with HTTP 200** |

The 8 product pages were chosen to span the cases that break naive parsers:

- `1847941834` — a book, so an ISBN-10 ASIN that is all digits
- `B0GR177QCS` — ₹1,72,490, exercising lakh-scale Indian digit grouping
- `B0DBVVW9XF` — no MRP at all, so no discount
- `B0F7X538TC` — "Only 1 left in stock." rather than plain "In stock"
- `B0C3ZYFZ77` — a 77% discount, the extreme end of the MRP/price ratio

`botcheck_search.html.gz` is the most valuable file in this directory. It is a
genuine block page, captured while Amazon was rate-limiting this machine. It
arrives with **HTTP 200**, so status codes cannot detect it, and it parses as
"zero results" — which is exactly the silent failure the bot-check detection
exists to prevent.

## Refreshing them

Amazon's markup drifts. When a fixture test starts failing for a legitimate
reason — not a parser bug — recapture:

```bash
uv run python - <<'EOF'
import asyncio, re, gzip, pathlib, sys
sys.path.insert(0, "src")
from amazon_cli.client.base import AmazonClient
from selectolax.parser import HTMLParser

OUT = pathlib.Path("tests/fixtures")

def trim(html):
    """Drop scripts/styles/comments: ~60% smaller, and no parser reads them."""
    t = HTMLParser(html)
    t.strip_tags(["script", "style", "noscript", "svg", "link"])
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"<!--.*?-->", "", t.html, flags=re.S))

async def main():
    async with AmazonClient() as c:
        for name, path in [("product_B0BZP2H373", "/dp/B0BZP2H373")]:
            html = await c.fetch(path)
            (OUT / f"{name}.html.gz").write_bytes(gzip.compress(trim(html).encode(), 9))
            print(name, len(html) // 1024, "KB")
            await asyncio.sleep(4)   # be polite, or you will capture a bot check

asyncio.run(main())
EOF
```

Two rules when recapturing:

1. **Sleep at least 3-4 seconds between fetches.** Capture too fast and you will
   collect bot-check pages instead of products — which is how
   `botcheck_search.html.gz` came to exist in the first place.
2. **Update `product_expected.json` in the same commit**, from an independent
   source (the live page in a browser), never by copying whatever the parser
   happened to produce. A fixture whose expectations come from the code under
   test asserts nothing.
