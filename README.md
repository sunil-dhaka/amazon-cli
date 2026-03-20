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
- **Customer insights** -- AI-generated review summary, aspect tags (Quality, Comfort, etc.), rating histogram
- **Compare** -- side-by-side comparison of 2+ products by ASIN
- **Reviews** -- top customer reviews with ratings, dates, and verified badges
- **Output modes** -- rich terminal tables (default), `--json` for scripting, `--plain` (TSV) for piping

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

### Compare products

```bash
amz compare B0DBVVW9XF B0DJMLWK7B B0CHP56CBB
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
  cli.py                 # click CLI group and command wiring
  output.py              # rich/json/plain formatters
  client/
    base.py              # async HTTP client (httpx) + ASIN validation
    parser.py            # selectolax HTML parsing + review insights
    types.py             # Product, ProductDetail, Review dataclasses
    search.py            # search via SSR
    product.py           # product detail + reviews via SSR
  commands/
    search.py            # amz search
    product.py           # amz product
    compare.py           # amz compare
    reviews.py           # amz reviews
```

## Limitations

- **Reviews are capped at ~13** -- Amazon.in requires login for the dedicated reviews page (`/product-reviews/`). The product page embeds about 13 top reviews, which is what `amz reviews` returns. The AI summary and aspect tags cover all reviews though.
- **Rate limiting** -- Rapid successive requests may trigger Amazon's bot detection (CAPTCHA). Normal usage (a few commands per minute) works fine.
- **Price availability** -- Some product pages don't show a price (out of stock, variant not selected). The CLI returns 0 in these cases.

## License

MIT
