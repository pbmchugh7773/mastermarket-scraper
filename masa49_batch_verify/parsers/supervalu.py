"""SuperValu parser — JSON-LD @graph primary, soft-404 detection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from ._jsonld import (
    find_product_node,
    find_webpage_node,
    first_offer_price,
    guess_size_from_text,
    image_url,
)

if TYPE_CHECKING:
    from batch_verify import ScrapeResult  # noqa: F401


def parse_supervalu(session: requests.Session, row: dict) -> "ScrapeResult":
    from batch_verify import ScrapeResult

    result = ScrapeResult(
        alias_id=row["alias_id"],
        store_name=row["store_name"],
        scraper_url=row["scraper_url"],
    )

    try:
        resp = session.get(row["scraper_url"], timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        result.scrape_status = "error"
        result.notes = f"network: {type(exc).__name__}"
        return result

    result.http_status = resp.status_code
    if resp.status_code == 404:
        result.scrape_status = "404_removed"
        return result
    if resp.status_code == 403:
        result.scrape_status = "bot_blocked"
        result.notes = "http 403"
        return result
    if resp.status_code >= 500:
        result.scrape_status = "error"
        result.notes = f"http {resp.status_code}"
        return result

    html = resp.text
    lower = html.lower()

    # SuperValu soft-404 is detected after JSON-LD parsing: if there's no
    # Product AND no WebPage with a real name, it's effectively removed.
    # Avoid broad substring matches on "we couldn't find" — that string appears
    # in the search-suggest JS of every live product page as a false positive.
    pass

    product = find_product_node(html)
    if not product:
        # Check for Cloudflare / bot-wall signatures
        if "cloudflare" in lower and "just a moment" in lower:
            result.scrape_status = "bot_blocked"
            return result

        # SuperValu renders price client-side — Product JSON-LD is often missing.
        # Fall back to the WebPage node which has the full product name, so VP Data
        # can still cross-reference even when price needs a Selenium pass.
        webpage = find_webpage_node(html)
        if webpage and webpage.get("name"):
            name = webpage["name"]
            if " - Storefront" in name:
                name = name.split(" - Storefront")[0]
            result.scraped_name = name.strip()
            result.scraped_size = guess_size_from_text(result.scraped_name)
            result.scrape_status = "parse_error"
            result.notes = "js_required_for_price (name+size from WebPage node)"
            return result

        result.scrape_status = "parse_error"
        result.notes = "no Product JSON-LD"
        return result

    result.scraped_name = product.get("name")
    brand = product.get("brand")
    result.scraped_brand = brand.get("name") if isinstance(brand, dict) else brand
    result.scraped_price = first_offer_price(product)
    result.scraped_image_url = image_url(product)

    # SuperValu typically encodes size in name: "Kilmeaden Mature Red Cheddar 200 g"
    result.scraped_size = guess_size_from_text(result.scraped_name or "")

    category = product.get("category")
    if isinstance(category, str):
        result.scraped_category = category

    if result.scraped_price is not None and result.scraped_name:
        result.scrape_status = "ok"
    else:
        result.scrape_status = "parse_error"
        result.notes = "missing price or name"

    return result
