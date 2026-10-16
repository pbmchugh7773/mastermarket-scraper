"""
Dunnes online-catalogue check (2026-10-16).

The Apify Dunnes actor returns a price even for products Dunnes no longer
sells online: their page says "SORRY, PRODUCT UNAVAILABLE OR MISSING FROM
YOUR STORE". On 2026-10-16, 134 of 1008 active Dunnes aliases (13%) were in
that state while getting a fresh price twice a week, and 78 of them had Dunnes
as the only store with a price. An anonymous user called the AI assistant a
liar over one of them (Options hot chocolate, "cheaper at Dunnes").

Dunnes' website (Mi9 Commerce, same platform as SuperValu) reads products from
a public JSON API:

    GET https://storefrontgateway.dunnesstoresgrocery.com/api/stores/{store}/products/{sku}
      200 → product JSON (price, available, promotions…)
      404 → "Product {sku} not found" — not in that store's catalogue

Prices are national (same in 100 of 103 stores in a 2026-10-16 check), but the
catalogue is per store, so "missing" requires a 404 at all of CHECK_STORES. The
`price` field is the regular shelf price WITHOUT temporary sale prices — use
this module for availability only, never as a price source.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import requests

STOREFRONT_API = "https://storefrontgateway.dunnesstoresgrocery.com/api/stores/{store}/products/{sku}"
# Beacon Court (site default), Cornelscourt (Dublin), Bandon Road (Cork).
CHECK_STORES = ("258", "207", "445")
MISSING_MESSAGE = "HTTP 404 (not in Dunnes online catalogue)"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; MasterMarket/1.0)"}
SKU_RE = re.compile(r"-id-(\d+)")


def dunnes_sku(url: Optional[str]) -> Optional[str]:
    match = SKU_RE.search(url or "")
    return match.group(1) if match else None


def _fetch(session, store: str, sku: str, timeout: int) -> Optional[int]:
    """HTTP status for one store, or None when the request itself failed."""
    for attempt in range(2):
        try:
            return session.get(STOREFRONT_API.format(store=store, sku=sku), headers=HEADERS, timeout=timeout).status_code
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


def catalogue_status(sku: Optional[str], session=requests, timeout: int = 20) -> str:
    """
    'listed'  — some store returns the product,
    'missing' — every CHECK_STORES store answers 404,
    'unknown' — no SKU, or an error/unexpected status; callers must fail open.
    """
    if not sku:
        return "unknown"
    for store in CHECK_STORES:
        status = _fetch(session, store, sku, timeout)
        if status == 200:
            return "listed"
        if status != 404:
            return "unknown"
    return "missing"
