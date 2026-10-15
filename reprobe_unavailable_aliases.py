#!/usr/bin/env python3
"""
Re-probe the aliases the dead-alias lifecycle retired, and restore live ones.

alias_lifecycle.py retires an alias after a week of dead-URL failures, and
retiring hides that store's prices for the product. Dead signals are not
always permanent — SuperValu's "Storefront EN" soft-404 also shows for
products that are only briefly off the online shelf — and nothing used to
look at a retired alias again: on 2026-10-14, 115 of 349 retired SuperValu
aliases were back on sale with a price.

For every alias with is_unavailable=True and an automatic preset
(alias_lifecycle.AUTO_RETIRE_PRESETS) this scrapes the URL again with the
store's normal scrape method. When it yields a price:
  1. PATCH /api/product-aliases/{id}/mark-available (back in the scrape pool),
  2. POST /api/scraping/update-status success=True (clears the old dead-URL
     streak, so one later blip cannot re-retire it at once).
The next regular scrape uploads a fresh price. Prices hidden at retirement
stay hidden; they are stale by then.

'manual_admin' / 'other' retirements are a human's call and never re-probed.
Tesco and Dunnes are scraped through Apify, so they are not re-probed here.

Proposal-only by default: writes a CSV and changes nothing. --apply restores.

Usage:
  python reprobe_unavailable_aliases.py                      # all stores, proposal
  python reprobe_unavailable_aliases.py --stores SuperValu --apply
  python reprobe_unavailable_aliases.py --alias-ids ids.csv  # only these aliases
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import simple_local_to_prod as slp
from alias_lifecycle import AUTO_RETIRE_PRESETS

logger = logging.getLogger(__name__)

PROBE_STORES = ("SuperValu", "Aldi", "Lidl")
SELENIUM_STORES = ("Aldi",)
CSV_FIELDS = (
    "alias_id", "store", "product_id", "alias_name", "unavailable_reason",
    "unavailable_at", "scraper_url", "verdict", "live_price", "restored",
)


def is_reprobe_candidate(alias: Dict) -> bool:
    """Retired by the lifecycle (not by a human) and still has a URL to try."""
    return (
        bool(alias.get("is_unavailable"))
        and alias.get("unavailable_reason") in AUTO_RETIRE_PRESETS
        and bool(alias.get("scraper_url"))
    )


def read_alias_ids(path: Path) -> Set[int]:
    """Alias ids from a CSV with an `alias_id` column, or one id per line."""
    text = path.read_text().strip().splitlines()
    if text and "alias_id" in text[0]:
        return {int(row["alias_id"]) for row in csv.DictReader(text) if row.get("alias_id")}
    return {int(line.strip()) for line in text if line.strip().isdigit()}


def probe(scraper: slp.SimpleLocalScraper, store: str, alias: Dict) -> Tuple[str, Optional[float]]:
    """(verdict, price) — verdict is 'alive' only when the store page has a price."""
    scraper._last_redirect_error = None
    scraper._last_failure_reason = None
    url, name = alias["scraper_url"], alias.get("alias_name", "?")
    promo = None
    if store == "SuperValu":
        price, promo = scraper.scrape_supervalu(url, name)
    elif store == "Aldi":
        price, promo = scraper.scrape_aldi(url, name)
    elif store == "Lidl":
        price = scraper.scrape_lidl(url, name)
    else:
        return "unsupported_store", None

    if promo and promo.get("product_removed"):
        return "still_removed", None
    if scraper._last_redirect_error:
        return "redirected", None
    if price:
        return "alive", float(price)
    if scraper._last_failure_reason:
        return f"dead ({scraper._last_failure_reason})", None
    return "no_price", None


def restore(scraper: slp.SimpleLocalScraper, alias_id: int) -> bool:
    response = scraper._authed_request(
        "patch", f"{slp.API_URL}/api/product-aliases/{alias_id}/mark-available", timeout=30
    )
    if response.status_code != 200:
        logger.warning(f"⚠️ mark-available failed for alias {alias_id}: {response.status_code} {response.text[:120]}")
        return False
    if not scraper.update_scraping_status(alias_id=alias_id, success=True):
        logger.warning(f"⚠️ alias {alias_id} restored but its old dead-URL status was not cleared")
    return True


def fetch_store_aliases(scraper: slp.SimpleLocalScraper, store: str) -> List[Dict]:
    response = scraper._authed_request(
        "get", f"{slp.API_URL}/api/product-aliases/store/{store}",
        params={"active_only": "false"}, timeout=60,
    )
    response.raise_for_status()
    return response.json()


def reprobe(
    scraper: slp.SimpleLocalScraper,
    stores: Iterable[str],
    apply: bool,
    only_ids: Optional[Set[int]] = None,
    limit: Optional[int] = None,
    delay: float = 1.0,
) -> List[Dict]:
    rows: List[Dict] = []
    for store in stores:
        candidates = [a for a in fetch_store_aliases(scraper, store) if is_reprobe_candidate(a)]
        if only_ids is not None:
            candidates = [a for a in candidates if a["id"] in only_ids]
        candidates.sort(key=lambda a: a["id"])
        if limit:
            candidates = candidates[:limit]
        logger.info(f"🔁 {store}: {len(candidates)} retired aliases to re-probe")
        if not candidates:
            continue

        if store in SELENIUM_STORES and scraper.driver is None:
            scraper.driver = scraper.setup_chrome()
            if scraper.driver is None:
                logger.error(f"❌ {store}: Chrome setup failed — skipping store")
                continue

        for i, alias in enumerate(candidates, 1):
            verdict, price = probe(scraper, store, alias)
            restored = bool(apply and verdict == "alive" and restore(scraper, alias["id"]))
            rows.append({
                "alias_id": alias["id"],
                "store": store,
                "product_id": alias.get("product_id"),
                "alias_name": alias.get("alias_name"),
                "unavailable_reason": alias.get("unavailable_reason"),
                "unavailable_at": alias.get("unavailable_at"),
                "scraper_url": alias.get("scraper_url"),
                "verdict": verdict,
                "live_price": f"{price:.2f}" if price else "",
                "restored": restored,
            })
            mark = "♻️ restored" if restored else verdict
            logger.info(f"[{store} {i}/{len(candidates)}] {alias.get('alias_name', '?')[:60]} → {mark}"
                        + (f" €{price:.2f}" if price else ""))
            time.sleep(delay)
    return rows


def write_csv(rows: List[Dict], path: Path) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: List[Dict], apply: bool) -> None:
    logger.info("")
    logger.info("SUMMARY")
    logger.info(f"  Mode:                 {'APPLY' if apply else 'proposal only (no writes)'}")
    for store in sorted({r['store'] for r in rows}):
        mine = [r for r in rows if r["store"] == store]
        alive = sum(1 for r in mine if r["verdict"] == "alive")
        restored = sum(1 for r in mine if r["restored"])
        logger.info(f"  {store:<12} re-probed={len(mine)} alive={alive} restored={restored}")
    logger.info(f"  Re-probed:            {len(rows)}")
    logger.info(f"  Alive (has a price):  {sum(1 for r in rows if r['verdict'] == 'alive')}")
    logger.info(f"  Restored:             {sum(1 for r in rows if r['restored'])}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stores", default=",".join(PROBE_STORES),
                        help=f"Comma-separated subset of {', '.join(PROBE_STORES)}")
    parser.add_argument("--apply", action="store_true", help="Restore aliases that have a price again")
    parser.add_argument("--alias-ids", type=Path, help="Only re-probe these alias ids (CSV with alias_id column, or one per line)")
    parser.add_argument("--limit", type=int, help="Max aliases per store")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default 1)")
    parser.add_argument("--out", type=Path, help="Proposal CSV path (default /tmp/reprobe_<timestamp>.csv)")
    args = parser.parse_args(argv)

    stores = [s.strip() for s in args.stores.split(",") if s.strip()]
    unknown = [s for s in stores if s not in PROBE_STORES]
    if unknown:
        parser.error(f"unsupported store(s): {', '.join(unknown)} — use {', '.join(PROBE_STORES)}")

    scraper = slp.SimpleLocalScraper()
    if not scraper.authenticate():
        logger.error("❌ Authentication failed — nothing re-probed")
        return 1
    only_ids = read_alias_ids(args.alias_ids) if args.alias_ids else None
    try:
        rows = reprobe(scraper, stores, args.apply, only_ids=only_ids, limit=args.limit, delay=args.delay)
    finally:
        if scraper.driver:
            scraper.driver.quit()

    out = args.out or Path(f"/tmp/reprobe_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv")
    write_csv(rows, out)
    logger.info(f"📝 Wrote {out}")
    summarize(rows, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
