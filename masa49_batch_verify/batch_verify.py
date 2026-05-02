"""
MASA-49 — one-time batch URL verification for deactivated product aliases.

Reads `inactive_aliases.csv`, dispatches each URL to the right per-store parser,
writes `ground_truth.csv` incrementally so the job is crash-safe and resumable.

Usage:
    python batch_verify.py --store tesco          # run one store
    python batch_verify.py --store all            # run everything
    python batch_verify.py --store tesco --limit 5  # smoke test

Output schema (CSV):
    alias_id, store_name, scraper_url, scrape_status, http_status,
    scraped_name, scraped_brand, scraped_size, scraped_price,
    scraped_image_url, scraped_category, notes, timestamp

scrape_status:
    ok           - all fields extracted and price validated
    404_removed  - URL no longer resolves to a product page
    bot_blocked  - Cloudflare / anti-bot challenge intercepted
    parse_error  - page loaded but extraction failed
    error        - network / unexpected error

The scrape is READ-ONLY. No prod DB writes.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

ROOT = Path(__file__).parent
INPUT_CSV = ROOT / "inactive_aliases.csv"
OUTPUT_CSV = ROOT / "output" / "ground_truth.csv"

OUTPUT_FIELDS = [
    "alias_id",
    "store_name",
    "scraper_url",
    "scrape_status",
    "http_status",
    "scraped_name",
    "scraped_brand",
    "scraped_size",
    "scraped_price",
    "scraped_image_url",
    "scraped_category",
    "notes",
    "timestamp",
]

STORE_NORMALISED = {
    "aldi": "aldi",
    "dunnes stores": "dunnes",
    "supervalu": "supervalu",
    "tesco": "tesco",
}

# Per-store delay (seconds) — respect existing cron rate limits
STORE_DELAYS = {
    "aldi": (1.0, 2.0),
    "dunnes": (2.0, 4.0),
    "supervalu": (0.5, 1.5),
    "tesco": (1.5, 3.0),
}

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


@dataclass
class ScrapeResult:
    alias_id: str
    store_name: str
    scraper_url: str
    scrape_status: str = "error"
    http_status: Optional[int] = None
    scraped_name: Optional[str] = None
    scraped_brand: Optional[str] = None
    scraped_size: Optional[str] = None
    scraped_price: Optional[float] = None
    scraped_image_url: Optional[str] = None
    scraped_category: Optional[str] = None
    notes: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


def load_input(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def load_done_ids(path: Path, include_bot_blocked: bool = False) -> set[str]:
    """Resumable: skip alias_ids already present in output CSV.

    When ``include_bot_blocked`` is True, rows previously marked
    ``scrape_status=bot_blocked`` are NOT considered done — the dispatcher will
    re-attempt them (intended for the Selenium/Apify backfill subpass per
    MASA-75). All other terminal statuses (``ok``, ``404_removed``,
    ``parse_error``, ``error``) remain skipped.
    """
    if not path.exists():
        return set()
    with path.open() as fh:
        reader = csv.DictReader(fh)
        if include_bot_blocked:
            return {
                row["alias_id"] for row in reader
                if row.get("scrape_status") != "bot_blocked"
            }
        return {row["alias_id"] for row in reader}


def append_result(path: Path, result: ScrapeResult) -> None:
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(asdict(result))


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IE,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    return s


def rate_limit(store: str) -> None:
    lo, hi = STORE_DELAYS.get(store, (1.0, 2.0))
    time.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Per-store parsers — registered in PARSERS dict
# ---------------------------------------------------------------------------

from parsers.tesco import parse_tesco  # noqa: E402
from parsers.aldi import parse_aldi  # noqa: E402
from parsers.supervalu import parse_supervalu  # noqa: E402
from parsers.dunnes import parse_dunnes  # noqa: E402

PARSERS: dict[str, Callable[[requests.Session, dict], ScrapeResult]] = {
    "tesco": parse_tesco,
    "aldi": parse_aldi,
    "supervalu": parse_supervalu,
    "dunnes": parse_dunnes,
}


def run(
    store_filter: str,
    limit: Optional[int],
    include_bot_blocked: bool = False,
) -> tuple[int, dict]:
    rows = load_input(INPUT_CSV)
    done = load_done_ids(OUTPUT_CSV, include_bot_blocked=include_bot_blocked)
    session = build_session()

    processed = 0
    status_counts: dict[str, int] = {}
    for row in rows:
        store_norm = STORE_NORMALISED.get(row["store_name"].lower())
        if store_norm is None:
            continue
        if store_filter != "all" and store_norm != store_filter:
            continue
        if row["alias_id"] in done:
            continue
        if limit is not None and processed >= limit:
            break

        parser = PARSERS[store_norm]
        print(f"[{processed + 1}] {store_norm} alias_id={row['alias_id']} -> {row['scraper_url'][:80]}")
        try:
            result = parser(session, row)
        except Exception as exc:  # pragma: no cover - defensive
            result = ScrapeResult(
                alias_id=row["alias_id"],
                store_name=row["store_name"],
                scraper_url=row["scraper_url"],
                scrape_status="error",
                notes=f"unhandled: {type(exc).__name__}: {exc}",
            )
        append_result(OUTPUT_CSV, result)
        status_counts[result.scrape_status] = status_counts.get(result.scrape_status, 0) + 1
        print(f"    -> {result.scrape_status} | name={result.scraped_name!r} price={result.scraped_price}")
        processed += 1
        rate_limit(store_norm)

    return processed, status_counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, choices=["all", "tesco", "aldi", "supervalu", "dunnes"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--include-bot-blocked",
        action="store_true",
        help=(
            "Re-attempt rows previously marked scrape_status=bot_blocked "
            "(MASA-75 subpass). Other terminal statuses remain skipped."
        ),
    )
    args = ap.parse_args()

    processed, counts = run(
        args.store,
        args.limit,
        include_bot_blocked=args.include_bot_blocked,
    )
    print(f"\nProcessed: {processed}")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
