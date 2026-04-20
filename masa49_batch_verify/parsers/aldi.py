"""Aldi.ie parser — JSON-LD primary, CSS fallback."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from ._jsonld import find_product_node, first_offer_price, guess_size_from_text, image_url

if TYPE_CHECKING:
    from batch_verify import ScrapeResult  # noqa: F401


def parse_aldi(session: requests.Session, row: dict) -> "ScrapeResult":
    from batch_verify import ScrapeResult

    result = ScrapeResult(
        alias_id=row["alias_id"],
        store_name=row["store_name"],
        scraper_url=row["scraper_url"],
    )

    try:
        resp = session.get(row["scraper_url"], timeout=20, allow_redirects=True)
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

    # Aldi rotates weekly offers — removed products give a listing/search
    if "page not found" in lower or "product is not available" in lower:
        result.scrape_status = "404_removed"
        return result

    product = find_product_node(html)
    if not product:
        result.scrape_status = "parse_error"
        result.notes = "no Product JSON-LD"
        return result

    result.scraped_name = product.get("name")
    brand = product.get("brand")
    result.scraped_brand = (
        brand.get("name") if isinstance(brand, dict) else brand
    )
    result.scraped_price = first_offer_price(product)
    result.scraped_image_url = image_url(product)

    # Aldi size is usually in `weight` or inside name
    weight = product.get("weight")
    if isinstance(weight, dict):
        val = weight.get("value")
        unit = weight.get("unitCode") or weight.get("unitText")
        if val and unit:
            result.scraped_size = f"{val}{unit}"
    if not result.scraped_size:
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
