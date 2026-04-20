"""Tesco.ie parser — JSON-LD @graph structure with mobile-UA fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from ._jsonld import find_product_node, first_offer_price, guess_size_from_text, image_url

if TYPE_CHECKING:
    from batch_verify import ScrapeResult  # noqa: F401


def parse_tesco(session: requests.Session, row: dict) -> "ScrapeResult":
    from batch_verify import ScrapeResult  # avoid circular import

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
        # Akamai anti-bot / Tesco Web Server block
        result.scrape_status = "bot_blocked"
        result.notes = "http 403 akamai"
        return result

    if resp.status_code >= 500:
        result.scrape_status = "error"
        result.notes = f"http {resp.status_code}"
        return result

    html = resp.text
    lower = html.lower()

    # Tesco bot page indicator — their error page has "error" in <title>
    title_match = html[:5000].lower().find("<title>")
    if title_match >= 0:
        end = html.lower().find("</title>", title_match)
        if end > 0 and "error" in html[title_match:end].lower() and "tesco" not in html[title_match:end].lower():
            result.scrape_status = "bot_blocked"
            result.notes = "error title"
            return result

    # Some Tesco removed products redirect to search — detect before parsing
    if "product not found" in lower or "sorry, we couldn" in lower:
        result.scrape_status = "404_removed"
        return result

    product = find_product_node(html)
    if not product:
        # No product JSON-LD — either bot-blocked or removed
        if "captcha" in lower or "incapsula" in lower or "please verify" in lower:
            result.scrape_status = "bot_blocked"
            return result
        result.scrape_status = "parse_error"
        result.notes = "no Product JSON-LD"
        return result

    result.scraped_name = product.get("name")
    result.scraped_brand = (
        product.get("brand", {}).get("name")
        if isinstance(product.get("brand"), dict)
        else product.get("brand")
    )
    result.scraped_price = first_offer_price(product)
    result.scraped_image_url = image_url(product)

    # Tesco puts size in name ("Tesco Semi-Skimmed Milk 2 Litre")
    result.scraped_size = guess_size_from_text(result.scraped_name or "")

    # Category from breadcrumbs JSON-LD (scan all nodes)
    # Simpler: leave blank for this pass — can be enriched later.
    result.scraped_category = None

    if result.scraped_price is not None and result.scraped_name:
        result.scrape_status = "ok"
    else:
        result.scrape_status = "parse_error"
        result.notes = "missing price or name"

    return result
