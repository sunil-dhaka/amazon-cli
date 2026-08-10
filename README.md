<p align="center">
  <img src="assets/banner.svg" alt="amz -- Amazon.in in your terminal" width="700">
</p>

<p align="center">
  <strong>Search, compare, and explore Amazon.in products from your terminal.</strong><br>
  No login. No API key. No browser. Just fast, structured product data.
</p>

---

A command-line interface for [Amazon.in](https://www.amazon.in) -- India's largest e-commerce marketplace. Search across millions of products, compare prices side by side, read customer reviews, and get AI-generated review insights, all without leaving the terminal.

Built on server-side rendered HTML parsing. Amazon embeds full product data in the page source -- `amz` fetches the page and extracts it using fast CSS selectors ([selectolax](https://github.com/rushter/selectolax)), no headless browser needed.

## Features

- **Search** -- full-text product search with sort (price, reviews, newest) and pagination
- **Product details** -- price, MRP, discount, brand, rating, features, specifications
- **Batch lookup** -- `amz product A B C` fetches concurrently; one bad ASIN never sinks the rest
- **Watch** -- a price watchlist with target prices and alerts, backed by SQLite ([below](#watching-prices))
- **Offers** -- the buy-box offer: price, who sells it, who ships it
- **Variants** -- every size/colour/style of a product, so you can spot a cheaper size
- **Deals & bestsellers** -- today's deals and ranked bestseller lists by category
- **Customer insights** -- AI-generated review summary, aspect tags (Quality, Comfort, etc.), rating histogram
- **Compare** -- side-by-side comparison of 2+ products by ASIN
- **Reviews** -- top customer reviews with ratings, dates, and verified badges
- **Output modes** -- rich tables (default), `--json`, `--plain` (TSV), `--csv`
- **Caching** -- `--cache 10m` reuses recent responses instead of refetching a 2 MB page
- **Politeness** -- retries with exponential backoff, `--min-interval` throttling, and it *tells you*
  when Amazon serves a bot check instead of silently reporting zero results

## Watching prices

The reason this tool exists. Add a product with the price you would happily pay,
then let cron tell you when it gets there.

```bash
amz watch add B0BZP2H373 --target 23000
amz watch list
amz watch check --quiet          # prints only what crossed the line
amz watch history B0BZP2H373     # price points + sparkline
```

`check` is cron-friendly: it prints nothing when nothing has happened, so
`0 * * * * amz watch check --quiet` mails you only on a real drop.

Alerts have hysteresis. The naive rule -- notify whenever `price <= target` --
fires once an hour for as long as a product stays cheap. `amz watch` remembers
the price it last told you about, stays quiet while the price hovers there,
speaks again only on a genuine new low, and re-arms when the price climbs back
above your target.

## Money is integer paise

Every price is an `int` count of paise. No `float`, no `Decimal`. A comparison
that decides whether to alert you should not be able to go wrong by a rounding
error, and `2599000 <= 2600000` cannot.

Rupee strings are produced only at the display edge, with Indian digit grouping:
`Rs.1,72,490`, never `Rs.172,490`. JSON output carries both -- `price` in rupees
for humans, `price_paise` for arithmetic.

## Installation

Requires **Python 3.12+**.

### As a global command (recommended)

Install once, use `amz` anywhere:

```bash
# Using uv (fastest)
uv tool install git+https://github.com/sunil-dhaka/amazon-cli.git

# Using pipx
pipx install git+https://github.com/sunil-dhaka/amazon-cli.git

# Using pip (installs into your current Python environment)
pip install git+https://github.com/sunil-dhaka/amazon-cli.git
```

After installation, `amz` is available as a command:

```bash
amz search "laptop"
amz product B09G9HD6PD
```

To uninstall: `uv tool uninstall amazon-cli` (or `pipx uninstall amazon-cli`).

### For development

```bash
git clone https://github.com/sunil-dhaka/amazon-cli.git
cd amazon-cli
uv sync
uv run amz --help
```

## Usage

### Search products

```bash
amz search "nike shoes"
amz search "laptops" --sort price_asc --page 2
amz search "headphones" --json
amz search "keyboards" --plain
```

Sort options: `relevance`, `price_asc`, `price_desc`, `reviews`, `newest`

### Product details

```bash
amz product B0DBVVW9XF
```

```
+------------------------------ B0DBVVW9XF --------------------------------+
| Nike Nike Mens Revolution 7 Running Shoes                                |
|                                                                          |
| Price: Rs.3,325 Rs.3,695 (10% off)                                      |
| Rating: 4.1 [****.] (1,910)                                             |
| Stock: In stock                                                          |
+--------------------------------------------------------------------------+
+----------------------------- Customer Insights --------------------------+
| Customers find these running shoes to be of good quality, comfortable,   |
| and lightweight, with soft padding and good value for money.             |
|                                                                          |
| Rating breakdown:                                                        |
|   5 star  ############################### 62%                            |
|   4 star  ########                  17%                                  |
|   ...                                                                    |
|                                                                          |
| What customers say:                                                      |
|   Quality               67 mentions  +59 -8                              |
|   Comfort               41 mentions  +38 -3                              |
|   Value For Money       18 mentions  +16 -2                              |
|   ...                                                                    |
+--------------------------------------------------------------------------+
```

### Several products at once

```bash
amz product B0BZP2H373 B0C3ZYFZ77 1847941834 --csv
```

Fetched concurrently (`--concurrency`, default 4). A bad ASIN is reported on
stderr and the rest still print; the command only fails if every lookup failed.

### Compare products

```bash
amz compare B0DBVVW9XF B0DJMLWK7B B0CHP56CBB
```

### Buying options and variants

```bash
amz offers B0BZP2H373
amz variants B0DBVVW9XF          # every size and colour, with their ASINs
```

### Deals and bestsellers

```bash
amz deals --min-discount 50 --limit 10
amz bestsellers electronics --limit 10
amz bestsellers --list-categories
```

### Watch a price

```bash
amz watch add B0BZP2H373 --target 23000
amz watch list
amz watch check --quiet
```

### Reviews

```bash
amz reviews B0DBVVW9XF
amz reviews B0DBVVW9XF --json
```

### JSON output

Every command supports `--json` for structured output:

```bash
amz product B0DBVVW9XF --json | jq '.insights.aspects'
amz search "shoes" --json | jq '.products[].price'

# Money comes in both forms: rupees to read, paise to compute with.
amz product B0BZP2H373 --json | jq '.price, .price_paise'
```

`--csv` is available on the same commands, and quotes titles containing commas
correctly (it uses Python's `csv` module, not string joining):

```bash
amz bestsellers electronics --csv > bestsellers.csv
```

### Caching and politeness

```bash
amz --cache 10m compare B0BZP2H373 B0C3ZYFZ77   # reuse recent pages
amz --min-interval 3 product A B C              # pace a batch
amz cache stats && amz cache clear --yes
```

## How it works

Amazon.in uses server-side rendering (SSR). When you request a product page, the HTML already contains all the data -- prices, specs, reviews, AI summaries. `amz` sends a single HTTP request with realistic browser headers, then parses the response using CSS selectors:

- `span#productTitle` -- product title
- `div#corePrice_feature_div` -- pricing
- `#histogramTable` -- rating distribution
- `[id^="rh_controls_aspect_"]` -- review aspect tags
- `[data-component-type="s-search-result"]` -- search results

No JavaScript execution, no headless browser, no API keys.

```
src/amazon_cli/
  cli.py                 # click group, global options, error boundary
  context.py             # resolved global options -> configured client
  money.py               # paise parsing/formatting, Indian digit grouping
  errors.py              # typed errors, each with a documented exit code
  cache.py               # on-disk response cache
  output.py              # rich/json/plain/csv formatters
  client/
    base.py              # async HTTP client, retries, bot-check detection
    parser.py            # product + search parsing
    types.py             # Product, ProductDetail, Offer, Variant, Deal, Review
    search.py  product.py  offers.py  variants.py  deals.py
  watch/
    store.py             # SQLite watchlist + price history
    service.py           # alert policy and the check sweep
  commands/              # one module per command
tests/
  fixtures/              # real captured Amazon.in pages (see its README)
  test_money.py  test_parser_fixtures.py  test_botcheck.py  ...
```

## Exit codes

Scripts can tell failures apart without scraping stderr:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error |
| 2 | Bad input (malformed ASIN, unknown category, invalid option) |
| 3 | Network failure or timeout |
| 4 | No such product or page |
| 5 | Amazon served a bot check, or is throttling |
| 6 | Page loaded but could not be parsed (markup drift) |

## Testing

The parser is the product, so it is tested against **real captured Amazon.in
pages** checked into `tests/fixtures/` -- eight product pages spanning a
numeric ISBN ASIN, a lakh-scale price, a product with no MRP and a low-stock
listing, plus search, deals, bestsellers, offers, variants, and a genuine
bot-check interstitial that Amazon served with HTTP 200.

```bash
uv run pytest
```

See `tests/fixtures/README.md` for what each fixture is and how to refresh it.

## Limitations

- **Reviews are capped at ~13** -- Amazon.in requires login for the dedicated reviews page (`/product-reviews/`). The product page embeds about 13 top reviews, which is what `amz reviews` returns. The AI summary and aspect tags cover all reviews though.
- **Rate limiting** -- Rapid successive requests trigger Amazon's bot detection. `amz` now detects that page and says so (exit code 5) instead of reporting zero results; use `--min-interval` to pace a batch, and `--cache` to avoid refetching.
- **Price availability** -- Some product pages don't show a price (out of stock, variant not selected). The CLI returns 0 in these cases.
- **Offers are the buy box only** -- Amazon loads its full third-party seller list over AJAX, and that endpoint is not reachable without a session. `amz offers` reports what is server-rendered: the buy-box price, seller and shipper.
- **Variant prices are usually blank** -- Amazon fetches sibling prices lazily, so `amz variants` lists the ASINs and options but rarely their prices. Run `amz product <ASIN>` on one to price it.
- **Deals are partly client-rendered** -- the deals page fills much of itself in with JavaScript, so `amz deals` sees only the server-rendered cards.

## License

MIT
