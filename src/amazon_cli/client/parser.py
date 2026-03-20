"""Amazon.in HTML parsing with selectolax."""

import re

from selectolax.parser import HTMLParser

from amazon_cli.client.types import (
    Product, ProductDetail, Review, ReviewAspect, ReviewInsights,
    _clean_text, _parse_price,
)


def parse_search_results(html: str) -> tuple[list[Product], int]:
    """Parse search results page. Returns (products, total_count)."""
    tree = HTMLParser(html)
    total_count = _parse_result_count(tree)

    products = []
    for node in tree.css('[data-component-type="s-search-result"]'):
        asin = node.attributes.get("data-asin", "")
        if not asin:
            continue
        if "AdHolder" in (node.attributes.get("class", "")):
            continue

        product = _parse_search_item(node, asin)
        if product:
            products.append(product)

    return products, total_count


def _parse_result_count(tree: HTMLParser) -> int:
    """Extract total result count from breadcrumb area."""
    node = tree.css_first("div.s-breadcrumb")
    if not node:
        node = tree.css_first("span[data-component-type='s-result-info-bar']")
    if not node:
        return 0
    text = node.text(strip=True)
    match = re.search(r"of\s+(?:over\s+)?([\d,]+)\s+results", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_search_item(node, asin: str) -> Product | None:
    """Parse a single search result div."""
    # Title: second h2 (first is brand, second is product title)
    title = ""
    title_node = node.css_first("h2.a-size-base-plus.a-spacing-none.a-color-base.a-text-normal")
    if title_node:
        title = title_node.text(strip=True)
    else:
        h2s = node.css("h2")
        if len(h2s) >= 2:
            title = h2s[1].text(strip=True)
        elif h2s:
            title = h2s[0].text(strip=True)

    if not title:
        return None

    # Price
    price = 0
    for price_node in node.css("span.a-price"):
        classes = price_node.attributes.get("class", "")
        if "a-text-price" not in classes:
            offscreen = price_node.css_first("span.a-offscreen")
            if offscreen:
                price = _parse_price(offscreen.text(strip=True))
            break

    # Rating
    rating = 0.0
    rating_node = node.css_first("span.a-icon-alt")
    if rating_node:
        match = re.search(r"([\d.]+)\s+out\s+of\s+5", rating_node.text(strip=True))
        if match:
            rating = float(match.group(1))

    # Review count
    review_count = 0
    for a_tag in node.css("a"):
        href = a_tag.attributes.get("href", "")
        if "customerReviews" in href:
            text = a_tag.text(strip=True).strip("()")
            review_count = _parse_count(text)
            break
    if not review_count:
        for span in node.css("span.a-size-base.s-underline-text"):
            text = span.text(strip=True).strip("()")
            count = _parse_count(text)
            if count:
                review_count = count
                break

    # Image
    image_url = ""
    img = node.css_first("img.s-image")
    if img:
        image_url = img.attributes.get("src", "")

    # Prime badge
    is_prime = bool(node.css_first("i.a-icon-prime"))

    # Delivery -- clean up concatenated text
    delivery = ""
    del_node = node.css_first("[data-cy='delivery-recipe']")
    if not del_node:
        del_node = node.css_first("[data-cy='delivery-block']")
    if del_node:
        delivery = _clean_delivery(del_node.text(strip=True))

    return Product(
        asin=asin,
        title=title,
        rating=rating,
        review_count=review_count,
        price=price,
        image_url=image_url,
        is_prime=is_prime,
        delivery=delivery,
    )


def parse_product_page(html: str, asin: str) -> ProductDetail:
    """Parse a product detail page."""
    tree = HTMLParser(html)

    # Title
    title = ""
    title_node = tree.css_first("span#productTitle")
    if title_node:
        title = title_node.text(strip=True)

    # Brand
    brand = ""
    brand_node = tree.css_first("a#bylineInfo")
    if not brand_node:
        for a in tree.css("a"):
            text = a.text(strip=True)
            if text.startswith("Visit the ") and text.endswith("Store"):
                brand_node = a
                break
    if brand_node:
        brand_text = brand_node.text(strip=True)
        brand = re.sub(r"^(Visit the |Brand:\s*)", "", brand_text)
        brand = re.sub(r"\s*Store$", "", brand)

    # Price
    price = 0
    for sel in [
        "div#corePrice_feature_div span.a-offscreen",
        "div#corePriceDisplay_desktop_feature_div span.a-offscreen",
        "span.priceToPay span.a-offscreen",
    ]:
        p_node = tree.css_first(sel)
        if p_node:
            price = _parse_price(p_node.text(strip=True))
            if price:
                break
    if not price:
        pw = tree.css_first("span.priceToPay span.a-price-whole")
        if pw:
            price = _parse_price(pw.text(strip=True))

    # MRP
    mrp = 0
    for sel in [
        "span.basisPrice span.a-offscreen",
        "span.a-text-price[data-a-strike='true'] span.a-offscreen",
        "span.a-price.a-text-price span.a-offscreen",
    ]:
        mrp_node = tree.css_first(sel)
        if mrp_node:
            mrp = _parse_price(mrp_node.text(strip=True))
            break

    # Discount
    discount = ""
    disc_node = tree.css_first("span.savingsPercentage")
    if disc_node:
        discount = disc_node.text(strip=True)

    # Rating
    rating = 0.0
    rating_node = tree.css_first("div#acrPopover")
    if rating_node:
        title_attr = rating_node.attributes.get("title", "")
        match = re.search(r"([\d.]+)\s+out\s+of\s+5", title_attr)
        if match:
            rating = float(match.group(1))
    if not rating:
        alt_node = tree.css_first("span.a-icon-alt")
        if alt_node:
            match = re.search(r"([\d.]+)\s+out\s+of\s+5", alt_node.text(strip=True))
            if match:
                rating = float(match.group(1))

    # Review count
    review_count = 0
    rc_node = tree.css_first("span#acrCustomerReviewText")
    if rc_node:
        review_count = _parse_count(rc_node.text(strip=True))

    # Availability -- extract just the status text, strip embedded JSON
    availability = ""
    avail_node = tree.css_first("div#availability")
    if avail_node:
        # Get only the first meaningful span's text
        span = avail_node.css_first("span")
        if span:
            availability = span.text(strip=True)
        else:
            availability = avail_node.text(strip=True)
        # Strip any JSON that leaked in
        availability = re.sub(r"\{.*", "", availability).strip()

    # Feature bullets
    features = []
    fb_node = tree.css_first("div#feature-bullets")
    if fb_node:
        for li in fb_node.css("li span.a-list-item"):
            text = _clean_text(li.text(strip=True))
            if text and not text.startswith("Show more"):
                features.append(text)

    # Specs -- try multiple Amazon page layouts
    specs = _parse_specs(tree)

    # Image
    image_url = ""
    img = tree.css_first("img#landingImage")
    if img:
        image_url = img.attributes.get("data-old-hires", "") or img.attributes.get("src", "")

    return ProductDetail(
        asin=asin,
        title=title,
        brand=brand,
        price=price,
        mrp=mrp,
        discount=discount,
        rating=rating,
        review_count=review_count,
        availability=availability,
        features=features,
        specs=specs,
        image_url=image_url,
        insights=_parse_review_insights(tree),
    )


def _parse_review_insights(tree: HTMLParser) -> ReviewInsights:
    """Extract AI summary, aspect tags, and rating histogram."""
    # AI summary: long span text starting with "Customers find..."
    summary = ""
    insights_node = tree.css_first('[data-csa-c-slot-id="cr-product-insights-detail-page"]')
    if insights_node:
        for span in insights_node.css("span"):
            text = span.text(strip=True)
            if len(text) > 80 and re.match(r"Customers\s+(find|appreciate|like|say)", text):
                summary = _clean_text(text)
                break

    # Aspect tags: divs with id="rh_controls_aspect_N"
    aspects = []
    for div in tree.css('[id^="rh_controls_aspect_"]'):
        text = div.text(strip=True)
        match = re.match(
            r"(\d+)\s+customers mention\s+(.+?),\s+(\d+)\s+positive,\s+(\d+)\s+negative",
            text,
        )
        if match:
            aspects.append(ReviewAspect(
                name=match.group(2).strip().title(),
                total=int(match.group(1)),
                positive=int(match.group(3)),
                negative=int(match.group(4)),
            ))

    # Rating histogram
    histogram = {}
    for a in tree.css("#histogramTable a"):
        label = a.attributes.get("aria-label", "")
        match = re.search(r"(\d+)\s+percent.*?(\d+)\s+star", label)
        if match:
            histogram[int(match.group(2))] = int(match.group(1))

    return ReviewInsights(summary=summary, aspects=aspects, histogram=histogram)


def _parse_specs(tree: HTMLParser) -> dict[str, str]:
    """Extract specifications from product page (multiple layout patterns)."""
    specs = {}

    # Pattern 1: prodDetTable (electronics, appliances)
    for table in tree.css("table.prodDetTable"):
        for tr in table.css("tr"):
            th = tr.css_first("th")
            td = tr.css_first("td")
            if th and td:
                key = _clean_text(th.text(strip=True))
                val = _clean_text(td.text(strip=True))
                if key and val and not _is_junk(val):
                    specs[key] = val

    # Pattern 2: productOverview table (common on many pages)
    if not specs:
        overview = tree.css_first("div#productOverview_feature_div table")
        if overview:
            for tr in overview.css("tr"):
                tds = tr.css("td")
                if len(tds) >= 2:
                    key = _clean_text(tds[0].text(strip=True))
                    val = _clean_text(tds[1].text(strip=True))
                    if key and val and not _is_junk(val):
                        specs[key] = val

    return specs


def parse_reviews_page(html: str) -> list[Review]:
    """Parse reviews from product page or dedicated reviews page."""
    tree = HTMLParser(html)
    reviews = []

    for node in tree.css('[data-hook="review"]'):
        # Rating
        rating = 0.0
        star_node = node.css_first('[data-hook="review-star-rating"] span.a-icon-alt')
        if not star_node:
            star_node = node.css_first('[data-hook="cmps-review-star-rating"] span.a-icon-alt')
        if star_node:
            match = re.search(r"([\d.]+)\s+out\s+of\s+5", star_node.text(strip=True))
            if match:
                rating = float(match.group(1))

        # Title
        title = ""
        title_node = node.css_first('[data-hook="review-title"]')
        if title_node:
            for span in title_node.css("span"):
                text = span.text(strip=True)
                if text and "out of" not in text:
                    title = text

        # Body
        body = ""
        body_node = node.css_first('[data-hook="review-body"]')
        if body_node:
            body = body_node.text(strip=True)
            body = re.sub(r"(Read more|Read less)$", "", body).rstrip()
            body = re.sub(r"^The media could not be loaded\.\s*", "", body)

        # Author
        author = ""
        author_node = node.css_first("span.a-profile-name")
        if author_node:
            author = author_node.text(strip=True)

        # Date
        date = ""
        date_node = node.css_first('[data-hook="review-date"]')
        if date_node:
            date_text = date_node.text(strip=True)
            match = re.search(r"on\s+(.+)$", date_text)
            date = match.group(1) if match else date_text

        # Verified
        verified = bool(node.css_first('[data-hook="avp-badge"]'))

        reviews.append(Review(
            title=title,
            body=body,
            rating=rating,
            author=author,
            date=date,
            verified=verified,
        ))

    return reviews


def _parse_count(text: str) -> int:
    """Parse count strings like '1,910', '1.9K', '25K'."""
    text = text.strip().strip("()").replace(",", "")
    if not text:
        return 0
    match = re.match(r"([\d.]+)\s*([KkMm])?", text)
    if not match:
        return 0
    num = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        num *= 1000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


def _clean_delivery(text: str) -> str:
    """Clean up concatenated delivery text from Amazon."""
    # "FREE deliveryTue, 24 Mar" -> "FREE delivery Tue, 24 Mar"
    text = re.sub(r"(delivery)([A-Z])", r"\1 \2", text)
    # "Or fastest deliverySun" -> "| Fastest: Sun..."
    text = re.sub(r"Or fastest delivery\s*", "| Fastest: ", text)
    # "on first order" -> strip
    text = re.sub(r"on (?:first |your )?order\s*$", "", text)
    return text.strip()


def _is_junk(text: str) -> bool:
    """Check if a specs value is JS/HTML junk rather than real data."""
    return any(marker in text for marker in ["window.", "function(", "var ", "P.when("])
