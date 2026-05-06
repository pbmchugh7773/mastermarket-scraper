"""
MASA-155 — Lidl sitemap → product-import pipeline (Phase 1.2 of MASA-137).

Implements the 3-gate validation contract from MASA-138 rules document:

    URL gate (pre-fetch)  → reject non-product URLs and already-imported aliases
    Payload gate (post-fetch) → parse JSON-LD Product, validate fields exist
    Insert gate (pre-DB-write) → category map, brand canonicalise, confidence score

Output (DRY-RUN ONLY — no DB writes):

    output/lidl_sitemap_proposals.csv     -- one row per accepted candidate
    output/lidl_sitemap_rejections.csv    -- one row per rejected URL with reason

The proposals CSV is attached to MASA-137 for board approval per MASA-138 §4.4
first-batch gate. A separate follow-up issue (post-approval) executes the
actual Product + ProductAlias INSERTs.

Skeleton status: structure + gates + constants from rules doc + primitives
imported from discover_lidl_common. JSON-LD parser and full gate logic are
TODO blocks marked NotImplementedError so the next heartbeat can fill them
in without re-deriving design.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from discover_lidl_common import (
    COMPETING_BRANDS,
    LIDL_OWN_BRANDS,
    _brand_mismatch_reason,
    _slug_brand_token,
    extract_page_text_signals,
    extract_size_from_html,
    fetch_lidl_page,
    fetch_lidl_sitemap_urls,
)

# ---------------------------------------------------------------------------
# Rules-doc constants — MASA-138 §1.1, §1.2, §4
# ---------------------------------------------------------------------------

# §1.1 — the 8 SEO hubs are the only legal Product.category values for new rows.
LIDL_HUB_SET: frozenset[str] = frozenset({
    "Food Cupboard",
    "Frozen Food",
    "Drinks",
    "Fresh Food",
    "Treats & Snacks",
    "Bakery",
    "Household",
    "Health & Beauty",
})

# §1.2 — Lidl breadcrumb leaf (lower-case) → MM hub.
# Single source of truth for category mapping; keep alphabetised within each hub.
LIDL_BREADCRUMB_TO_HUB: dict[str, str] = {
    # Bakery
    "bakery": "Bakery",
    "bread": "Bakery",
    "croissants": "Bakery",
    "pastries": "Bakery",
    "rolls": "Bakery",
    "wraps & flatbreads": "Bakery",
    # Fresh Food
    "cheese": "Fresh Food",
    "chicken & poultry": "Fresh Food",
    "chilled drinks": "Fresh Food",
    "chilled ready meals": "Fresh Food",
    "dairy": "Fresh Food",
    "deli": "Fresh Food",
    "eggs": "Fresh Food",
    "fish & seafood": "Fresh Food",
    "fresh meat": "Fresh Food",
    "fruit": "Fresh Food",
    "salad": "Fresh Food",
    "vegetables": "Fresh Food",
    "yoghurts": "Fresh Food",
    # Frozen Food
    "frozen desserts": "Frozen Food",
    "frozen fish": "Frozen Food",
    "frozen meat": "Frozen Food",
    "frozen pizza": "Frozen Food",
    "frozen ready meals": "Frozen Food",
    "frozen vegetables": "Frozen Food",
    "ice cream": "Frozen Food",
    # Food Cupboard
    "baking": "Food Cupboard",
    "cereals": "Food Cupboard",
    "coffee": "Food Cupboard",
    "condiments": "Food Cupboard",
    "cooking sauces": "Food Cupboard",
    "herbs & spices": "Food Cupboard",
    "oils & vinegars": "Food Cupboard",
    "pasta": "Food Cupboard",
    "rice & grains": "Food Cupboard",
    "spreads": "Food Cupboard",
    "tea": "Food Cupboard",
    "tinned & jarred": "Food Cupboard",
    "world food": "Food Cupboard",
    # Treats & Snacks
    "biscuits": "Treats & Snacks",
    "cakes": "Treats & Snacks",
    "chocolate": "Treats & Snacks",
    "crisps": "Treats & Snacks",
    "desserts": "Treats & Snacks",
    "nuts": "Treats & Snacks",
    "popcorn": "Treats & Snacks",
    "snacks": "Treats & Snacks",
    "sweets": "Treats & Snacks",
    # Drinks
    "beer": "Drinks",
    "cider": "Drinks",
    "energy drinks": "Drinks",
    "juices": "Drinks",
    "mixers": "Drinks",
    "soft drinks": "Drinks",
    "spirits": "Drinks",
    "water": "Drinks",
    "wine": "Drinks",
    # Household
    "air fresheners": "Household",
    "cleaning": "Household",
    "dishwashing": "Household",
    "kitchen roll": "Household",
    "laundry": "Household",
    "paper goods": "Household",
    "pet care": "Household",
    "pet food": "Household",
    "toilet paper": "Household",
    # Health & Beauty
    "baby care": "Health & Beauty",
    "body wash": "Health & Beauty",
    "cosmetics": "Health & Beauty",
    "deodorants": "Health & Beauty",
    "health": "Health & Beauty",
    "oral care": "Health & Beauty",
    "personal care": "Health & Beauty",
    "shampoo & conditioner": "Health & Beauty",
    "skincare": "Health & Beauty",
    "vitamins": "Health & Beauty",
}

# §4.2 — confidence scoring weights.
CONFIDENCE_WEIGHTS = {
    "well_formed_product_jsonld": 0.50,
    "breadcrumb_unambiguous": 0.20,
    "size_from_jsonld": 0.15,
    "brand_in_lidl_own_brands": 0.10,
    "image_host_whitelisted": 0.05,
}

# §4.3 — thresholds.
CONFIDENCE_AUTO_ACCEPT = 0.85
CONFIDENCE_HUMAN_REVIEW = 0.60

# Lidl product URL pattern: /p/{slug}/p{digits}.
LIDL_PRODUCT_URL_RE = re.compile(r"/p/[^/]+/p(\d+)/?$")

# JSON-LD detection — `<script type="application/ld+json">…</script>`.
JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.S | re.I,
)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
PROPOSALS_CSV = OUTPUT_DIR / "lidl_sitemap_proposals.csv"
REJECTIONS_CSV = OUTPUT_DIR / "lidl_sitemap_rejections.csv"


@dataclass
class ImportProposal:
    """Accepted candidate ready for board review per MASA-138 §6."""

    proposed_name: str
    proposed_brand: str
    proposed_category: str
    proposed_unit: str
    image_url: str
    json_ld_price: Optional[float]
    sitemap_url: str
    confidence: float
    top_rejection_reasons_if_any: str = ""  # populated for borderline scores


@dataclass
class RejectionRecord:
    """One row per dropped URL — fuels MASA-114 yield audit."""

    url: str
    gate: str  # "url" | "payload" | "insert"
    reason_code: str
    json_ld_excerpt: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


@dataclass
class _Candidate:
    """Internal carrier between gates — not part of public output."""

    url: str
    sku: str
    raw_html: str = ""
    json_ld: dict = field(default_factory=dict)
    breadcrumb_leaf: str = ""
    name: str = ""
    brand: str = ""
    image_url: str = ""
    price: Optional[float] = None
    size_text: str = ""
    confidence_features: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate 1 — URL gate (pre-fetch)
# ---------------------------------------------------------------------------

def url_gate(url: str, already_imported_skus: set[str]) -> tuple[Optional[_Candidate], Optional[RejectionRecord]]:
    """
    Pre-fetch validation per MASA-138 §5 (URL gate).

    Rejects:
      - bad_url_pattern   — URL does not match `/p/{slug}/p{digits}`
      - already_imported  — sku already in MM ProductAlias.scraper_url
    """
    m = LIDL_PRODUCT_URL_RE.search(url)
    if not m:
        return None, RejectionRecord(url=url, gate="url", reason_code="bad_url_pattern")
    sku = m.group(1)
    if sku in already_imported_skus:
        return None, RejectionRecord(url=url, gate="url", reason_code="already_imported")
    return _Candidate(url=url, sku=sku), None


# ---------------------------------------------------------------------------
# Gate 2 — Payload gate (post-fetch)
# ---------------------------------------------------------------------------

def _parse_jsonld_product(html: str) -> Optional[dict]:
    """
    Extract the Product JSON-LD block. Iterates all `application/ld+json`
    script tags and returns the first one with `@type: Product` (or a graph
    entry of that type). Returns None if no Product node found.

    TODO(MASA-155): handle `@graph` nesting + multiple Product blocks
    (variant pages). For skeleton, naive single-pass.
    """
    for match in JSONLD_SCRIPT_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Single Product node
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
        # @graph nesting
        if isinstance(data, dict):
            for node in data.get("@graph", []) or []:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
    return None


def payload_gate(candidate: _Candidate) -> tuple[Optional[_Candidate], Optional[RejectionRecord]]:
    """
    Post-fetch validation per MASA-138 §5 (payload gate).

    Rejects:
      - not_product_jsonld  — page has no recognised Product JSON-LD
      - no_price            — Product has no `offers.price`
      - missing_image       — Product has no `image`
      - expired_offer       — page renders sold-out / unavailable copy
      - noindex_meta        — page is `<meta name="robots" content="noindex">`

    Mutates candidate with parsed fields on success.
    """
    raise NotImplementedError(
        "MASA-155 skeleton: payload gate body. "
        "Implementation: call _parse_jsonld_product, extract name/brand/"
        "offers.price/image, check expired_offer/noindex via "
        "extract_page_text_signals, populate candidate fields."
    )


# ---------------------------------------------------------------------------
# Gate 3 — Insert gate (pre-DB-write)
# ---------------------------------------------------------------------------

def insert_gate(candidate: _Candidate) -> tuple[Optional[ImportProposal], Optional[RejectionRecord]]:
    """
    Insert gate per MASA-138 §5.

    Rejects:
      - unknown_brand              — brand not in LIDL_OWN_BRANDS / KNOWN_BRAND_TOKENS
      - competing_brand_in_slug    — slug carries a brand from COMPETING_BRANDS
                                     (Vemondo→Alpro class — see MASA-135)
      - image_host_not_whitelisted — image_url host not in next.config.js allowlist
      - size_source_conflict       — JSON-LD size disagrees with slug-derived size
      - unknown_breadcrumb_leaf    — breadcrumb leaf not in LIDL_BREADCRUMB_TO_HUB

    Computes confidence per §4.2 weights. Returns ImportProposal on success.
    """
    raise NotImplementedError(
        "MASA-155 skeleton: insert gate body. "
        "Implementation: brand canonicalise via LIDL_OWN_BRANDS, "
        "_brand_mismatch_reason, breadcrumb→hub via LIDL_BREADCRUMB_TO_HUB, "
        "image host check, confidence score from CONFIDENCE_WEIGHTS."
    )


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

PROPOSAL_FIELDS = list(ImportProposal.__dataclass_fields__.keys())
REJECTION_FIELDS = list(RejectionRecord.__dataclass_fields__.keys())


def _write_csv(path: Path, fields: list[str], row_dict: dict) -> None:
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row_dict)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(limit: Optional[int], dry_run: bool) -> tuple[int, dict]:
    """
    Iterate sitemap → 3 gates → write CSVs.

    Returns (processed, status_counts). `dry_run=True` is the only supported
    mode for Phase 1.2 — DB INSERTs are explicitly out of scope per MASA-155.
    """
    if not dry_run:
        raise SystemExit(
            "ERROR: only --dry-run is supported in Phase 1.2 per MASA-155 spec. "
            "DB INSERTs require board approval (MASA-138 §4.4) and are tracked "
            "as a separate follow-up issue."
        )

    # TODO(MASA-155): query MM DB for existing Lidl ProductAlias.scraper_url
    # SKUs to populate already_imported_skus. For skeleton, empty set so all
    # URLs are eligible (rejections will then be telemetry-only).
    already_imported_skus: set[str] = set()

    fetch_log: dict = {}
    sitemap_urls = fetch_lidl_sitemap_urls()
    counts: dict[str, int] = {"accepted": 0}
    processed = 0

    for url in sitemap_urls:
        if limit is not None and processed >= limit:
            break
        processed += 1

        # Gate 1
        candidate, rejection = url_gate(url, already_imported_skus)
        if rejection is not None:
            counts[rejection.reason_code] = counts.get(rejection.reason_code, 0) + 1
            _write_csv(REJECTIONS_CSV, REJECTION_FIELDS, asdict(rejection))
            continue

        # Fetch
        candidate.raw_html = fetch_lidl_page(url, fetch_log) or ""
        if not candidate.raw_html:
            rec = RejectionRecord(url=url, gate="payload", reason_code="fetch_failed")
            counts[rec.reason_code] = counts.get(rec.reason_code, 0) + 1
            _write_csv(REJECTIONS_CSV, REJECTION_FIELDS, asdict(rec))
            continue

        # Gate 2 (skeleton: NotImplementedError)
        candidate, rejection = payload_gate(candidate)
        if rejection is not None:
            counts[rejection.reason_code] = counts.get(rejection.reason_code, 0) + 1
            _write_csv(REJECTIONS_CSV, REJECTION_FIELDS, asdict(rejection))
            continue

        # Gate 3 (skeleton: NotImplementedError)
        proposal, rejection = insert_gate(candidate)
        if rejection is not None:
            counts[rejection.reason_code] = counts.get(rejection.reason_code, 0) + 1
            _write_csv(REJECTIONS_CSV, REJECTION_FIELDS, asdict(rejection))
            continue

        counts["accepted"] += 1
        _write_csv(PROPOSALS_CSV, PROPOSAL_FIELDS, asdict(proposal))

    return processed, counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Lidl sitemap import pipeline (MASA-155)")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N sitemap URLs (smoke testing).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Output proposals + rejections to CSV; no DB writes. "
             "(Phase 1.2 has no other mode — DB INSERTs require board approval.)",
    )
    args = ap.parse_args()

    processed, counts = run(limit=args.limit, dry_run=args.dry_run)
    print(f"\nProcessed: {processed}")
    print(f"Accepted: {counts.get('accepted', 0)}")
    print("Rejections by reason:")
    for k, v in sorted(counts.items()):
        if k == "accepted":
            continue
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
