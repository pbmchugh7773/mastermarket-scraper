"""Dunnes Stores parser — Cloudflare protected, requests-first attempt.

Notes:
- Dunnes blocks pure `requests` traffic via Cloudflare. When blocked we record
  `bot_blocked` so the batch can be re-run with a Cloudflare-bypassing client
  (cloudscraper / Playwright) without re-scraping the stores that worked.
- Known Apify bug: price=1 must be rejected and reported as parse_error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from ._jsonld import find_product_node, first_offer_price, guess_size_from_text, image_url

if TYPE_CHECKING:
    from batch_verify import ScrapeResult  # noqa: F401


DUNNES_PRICE_FLOOR = 0.05  # anything <=0.05 is an Apify-style bug artefact


def parse_dunnes(session: requests.Session, row: dict) -> "ScrapeResult":
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
        # Cloudflare Forbidden — bot wall
        result.scrape_status = "bot_blocked"
        result.notes = "cloudflare 403"
        return result
    if resp.status_code >= 500:
        result.scrape_status = "error"
        result.notes = f"http {resp.status_code}"
        return result

    html = resp.text
    lower = html.lower()

    if "just a moment" in lower and "cloudflare" in lower:
        result.scrape_status = "bot_blocked"
        result.notes = "cloudflare challenge"
        return result

    if "page not found" in lower or "item is no longer available" in lower:
        result.scrape_status = "404_removed"
        return result

    product = find_product_node(html)
    if not product:
        result.scrape_status = "parse_error"
        result.notes = "no Product JSON-LD"
        return result

    result.scraped_name = product.get("name")
    brand = product.get("brand")
    result.scraped_brand = brand.get("name") if isinstance(brand, dict) else brand
    price = first_offer_price(product)

    if price is not None and price <= DUNNES_PRICE_FLOOR:
        result.scrape_status = "parse_error"
        result.notes = f"suspected apify bug: price={price}"
        result.scraped_image_url = image_url(product)
        result.scraped_size = guess_size_from_text(result.scraped_name or "")
        return result

    result.scraped_price = price
    result.scraped_image_url = image_url(product)
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
