# amz -- Amazon.in in your terminal

A command-line interface for [Amazon.in](https://www.amazon.in). Search products, compare prices, view product details, and read reviews -- all from your terminal. No login required.

```
$ amz search "nike shoes"

                    Search Results (page 1, ~322 found)
  #  ASIN          Title                                   Price  Rating       Reviews
  1  B0DBVVW9XF    Nike Mens Revolution 7 Running Shoes  Rs.3,325  4.1 [****.]   1,900
  2  B0FFTFDWXF    Nike Mens Downshifter 14 Running...   Rs.4,895  3.8 [***+.]      13
  3  B0GKFYDGPK    Nike Mens Air Max Dawn Running...     Rs.5,817  4.2 [****.]      32
  ...
```

## Features

- **Search** -- full-text product search with sort and pagination
- **Product details** -- price, MRP, discount, brand, rating, features, specs
- **Compare** -- side-by-side comparison of 2+ products by ASIN
- **Reviews** -- read top customer reviews for any product

**Output modes:** rich terminal tables (default), `--json` for scripting, `--plain` for piping.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sunil-dhaka/amazon-cli.git
cd amazon-cli
uv sync
uv run amz --help
```

## Usage

### Search products

```bash
amz search "laptops"
amz search "nike shoes" --sort price_asc --page 2
amz search "headphones" --json          # structured JSON output
amz search "keyboards" --plain          # TSV for piping
```

Sort options: `relevance`, `price_asc`, `price_desc`, `reviews`, `newest`

### Product details

```bash
amz product B09G9HD6PD
amz product B09G9HD6PD --json
```

```
+------------------------------- B09G9HD6PD --------------------------------+
| Nike Nike Mens Revolution 7 Running Shoes                                  |
|                                                                            |
| Price: Rs.3,325 Rs.3,695 (10% off)                                        |
| Rating: 4.1 [****.] (1,910)                                               |
| Stock: In stock                                                            |
|                                                                            |
| Specifications:                                                            |
|   Brand: Nike                                                              |
|   Maximum Weight Recommendation: 135 Kilograms                            |
|   ...                                                                      |
+----------------------------------------------------------------------------+
```

### Compare products

```bash
amz compare B09G9HD6PD B0DJMLWK7B B0CHP56CBB
```

### Reviews

```bash
amz reviews B09G9HD6PD
amz reviews B09G9HD6PD --json
```

## How it works

All data comes from Amazon.in's server-side rendered HTML pages -- no API keys, no authentication, no browser automation. `amz` fetches the page with realistic browser headers and parses the HTML using [selectolax](https://github.com/rushter/selectolax) (a fast CSS selector engine built on Cython).

Search results are extracted from `[data-component-type="s-search-result"]` divs. Product details come from well-known element IDs (`#productTitle`, `#corePrice_feature_div`, `#feature-bullets`, etc.). Reviews are parsed from `[data-hook="review"]` elements embedded in the product page.

```
src/amazon_cli/
  cli.py                 # click CLI group and command wiring
  output.py              # rich/json/plain formatters
  client/
    base.py              # async HTTP client (httpx)
    parser.py            # selectolax HTML parsing
    types.py             # Product, ProductDetail, Review dataclasses
    search.py            # search via SSR
    product.py           # product detail + reviews via SSR
  commands/
    search.py            # amz search
    product.py           # amz product
    compare.py           # amz compare
    reviews.py           # amz reviews
```

## License

MIT
