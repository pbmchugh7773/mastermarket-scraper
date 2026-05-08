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

from urllib.parse import urlparse

from discover_lidl_common import (
    COMPETING_BRANDS,
    KNOWN_BRAND_TOKENS,
    LIDL_OWN_BRANDS,
    _brand_mismatch_reason,
    _slug_brand_token,
    extract_page_text_signals,
    extract_size_from_html,
    fetch_lidl_page,
    fetch_lidl_sitemap_urls,
    normalise,
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
LIDL_PRODUCT_URL_RE = re.compile(r"/p/(?P<slug>[^/]+)/p(?P<sku>\d+)/?$")

# §5 image-host whitelist.
#
# Source of truth is `web/next.config.js` `images.domains` in the MasterMarket
# main repo — this scraper repo does not have direct access to it. Any host
# added here MUST also be added there before the first Phase-1.3 INSERT, or
# the new Lidl rows will render as broken images on the web frontend.
#
# The Lidl IE storefront serves all product images via the Schwarz Group
# imgproxy CDN (verify empirically with `curl -sI` on a real product page;
# do not infer from marketing-domain — see feedback memory 2026-04-29).
LIDL_IMAGE_HOST_WHITELIST: frozenset[str] = frozenset({
    "imgproxy-retcat.assets.schwarz",
})

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
    sku = m.group("sku")
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


# Patterns for §5 payload-gate HTML checks.
NOINDEX_META_RE = re.compile(
    r'<meta[^>]+name=["\']robots["\'][^>]+content=["\'][^"\']*noindex',
    re.I,
)
EXPIRED_OFFER_PHRASES = (
    "currently unavailable",
    "out of stock",
    "this offer has ended",
)


def _extract_brand(jsonld: dict) -> str:
    """Brand can be a string or a `{"@type": "Brand", "name": "..."}` object."""
    brand = jsonld.get("brand")
    if isinstance(brand, str):
        return brand.strip()
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str):
            return name.strip()
    if isinstance(brand, list) and brand:
        head = brand[0]
        if isinstance(head, str):
            return head.strip()
        if isinstance(head, dict):
            name = head.get("name")
            if isinstance(name, str):
                return name.strip()
    return ""


def _extract_image(jsonld: dict) -> str:
    """`image` may be string, list of strings, or list of ImageObject dicts."""
    image = jsonld.get("image")
    if isinstance(image, str):
        return image.strip()
    if isinstance(image, list) and image:
        head = image[0]
        if isinstance(head, str):
            return head.strip()
        if isinstance(head, dict):
            url = head.get("url") or head.get("contentUrl")
            if isinstance(url, str):
                return url.strip()
    if isinstance(image, dict):
        url = image.get("url") or image.get("contentUrl")
        if isinstance(url, str):
            return url.strip()
    return ""


def _extract_price(jsonld: dict) -> Optional[float]:
    """`offers.price` may be number-as-string under `offers` or nested in `priceSpecification`."""
    offers = jsonld.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        return None
    raw = offers.get("price")
    if raw is None:
        spec = offers.get("priceSpecification")
        if isinstance(spec, dict):
            raw = spec.get("price")
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _extract_breadcrumb_leaf(jsonld: dict, html: str) -> str:
    """
    Prefer JSON-LD `category` on the Product node. Fallback to scanning the
    page for a separate `BreadcrumbList` script. Returns lower-case leaf.
    """
    cat = jsonld.get("category")
    if isinstance(cat, str) and cat.strip():
        # Path-style category like "Bakery > Bread" → take last segment.
        leaf = cat.strip().rsplit(">", 1)[-1].strip().lower()
        return leaf
    # Fallback: BreadcrumbList in any other JSON-LD script.
    for match in JSONLD_SCRIPT_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = [data] if isinstance(data, dict) else []
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates.extend(n for n in data["@graph"] if isinstance(n, dict))
        for node in candidates:
            if node.get("@type") == "BreadcrumbList":
                items = node.get("itemListElement") or []
                if isinstance(items, list) and items:
                    last = items[-1]
                    if isinstance(last, dict):
                        item = last.get("item") or last.get("name")
                        if isinstance(item, dict):
                            item = item.get("name") or ""
                        if isinstance(item, str) and item.strip():
                            return item.strip().lower()
    return ""


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
    html = candidate.raw_html
    # noindex check first — cheapest, applies regardless of JSON-LD presence.
    if NOINDEX_META_RE.search(html):
        return None, RejectionRecord(
            url=candidate.url, gate="payload", reason_code="noindex_meta"
        )

    jsonld = _parse_jsonld_product(html)
    if jsonld is None:
        return None, RejectionRecord(
            url=candidate.url, gate="payload", reason_code="not_product_jsonld"
        )
    candidate.json_ld = jsonld

    image = _extract_image(jsonld)
    if not image:
        return None, RejectionRecord(
            url=candidate.url, gate="payload", reason_code="missing_image",
            json_ld_excerpt=json.dumps({k: jsonld.get(k) for k in ("@type", "name")})[:500],
        )

    price = _extract_price(jsonld)
    if price is None:
        return None, RejectionRecord(
            url=candidate.url, gate="payload", reason_code="no_price",
            json_ld_excerpt=json.dumps({"offers": jsonld.get("offers")})[:500],
        )

    # Expired-offer check on visible page text, not raw HTML
    # (avoids false positives in script tags or HTML comments).
    signals = extract_page_text_signals(html)
    text_lower = signals.get("text", "").lower()
    for phrase in EXPIRED_OFFER_PHRASES:
        if phrase in text_lower:
            return None, RejectionRecord(
                url=candidate.url, gate="payload", reason_code="expired_offer",
                json_ld_excerpt=phrase,
            )

    candidate.image_url = image
    candidate.price = price
    name = jsonld.get("name")
    candidate.name = name.strip() if isinstance(name, str) else ""
    candidate.brand = _extract_brand(jsonld)
    candidate.breadcrumb_leaf = _extract_breadcrumb_leaf(jsonld, html)
    candidate.size_text = extract_size_from_html(html) or ""
    return candidate, None


# ---------------------------------------------------------------------------
# Gate 3 — Insert gate (pre-DB-write)
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    """Extract `{slug}` from `/p/{slug}/p{digits}` URL pattern."""
    m = LIDL_PRODUCT_URL_RE.search(url)
    return m.group("slug") if m else ""


def _brand_is_known(brand: str) -> bool:
    """
    True if `brand` (after normalisation) appears in LIDL_OWN_BRANDS or
    any other KNOWN_BRAND_TOKENS entry. Multi-word brands ("coca cola")
    match as space-separated tokens after normalise().
    """
    if not brand:
        return False
    brand_norm = normalise(brand)
    if not brand_norm:
        return False
    # Exact match against the canonical brand list (post-normalisation).
    for token in KNOWN_BRAND_TOKENS:
        if normalise(token) == brand_norm:
            return True
    return False


def _brand_in_lidl_own_brands(brand: str) -> bool:
    if not brand:
        return False
    brand_norm = normalise(brand)
    return any(normalise(b) == brand_norm for b in LIDL_OWN_BRANDS)


def _image_host(image_url: str) -> str:
    try:
        return urlparse(image_url).hostname or ""
    except (ValueError, TypeError):
        return ""


def _compute_confidence(candidate: _Candidate, breadcrumb_hub: str) -> tuple[float, dict]:
    """
    Apply MASA-138 §4.2 weights. Returns (score, features) where features is
    a debug-friendly dict of which signals contributed.

    `well_formed_product_jsonld` is implicitly true because we got past
    payload_gate; we still record it explicitly for the rejection-record
    audit trail.
    `size_from_jsonld` is currently always False — Phase-1.2 extracts size
    from HTML signals, not JSON-LD weight/volume. Wired here so the next
    chunk that adds JSON-LD size extraction flips the bit without touching
    this scoring function.
    """
    features = {
        "well_formed_product_jsonld": True,
        "breadcrumb_unambiguous": bool(breadcrumb_hub),
        "size_from_jsonld": False,  # TODO: flip when JSON-LD size extracted
        "brand_in_lidl_own_brands": _brand_in_lidl_own_brands(candidate.brand),
        "image_host_whitelisted": _image_host(candidate.image_url) in LIDL_IMAGE_HOST_WHITELIST,
    }
    score = sum(CONFIDENCE_WEIGHTS[k] for k, present in features.items() if present)
    return round(score, 2), features


def insert_gate(candidate: _Candidate) -> tuple[Optional[ImportProposal], Optional[RejectionRecord]]:
    """
    Insert gate per MASA-138 §5.

    Rejects:
      - unknown_breadcrumb_leaf    — breadcrumb leaf not in LIDL_BREADCRUMB_TO_HUB
      - unknown_brand              — brand not in LIDL_OWN_BRANDS / KNOWN_BRAND_TOKENS
      - competing_brand_in_slug    — slug carries a different brand than the
                                     JSON-LD-declared brand (Vemondo→Alpro class)
      - image_host_not_whitelisted — image_url host not in LIDL_IMAGE_HOST_WHITELIST
      - size_source_conflict       — JSON-LD size disagrees with slug-derived size
                                     (TODO: needs JSON-LD weight/volume extraction)

    Order is cheapest-first to short-circuit on the most common rejects.
    Computes confidence per §4.2 weights. Returns ImportProposal on success.
    """
    # 1. Breadcrumb → hub. Cheapest dict lookup; rejects free-text categories
    #    like "Lidl Surprises" that the seed table doesn't cover.
    hub = LIDL_BREADCRUMB_TO_HUB.get(candidate.breadcrumb_leaf)
    if hub is None:
        return None, RejectionRecord(
            url=candidate.url, gate="insert", reason_code="unknown_breadcrumb_leaf",
            json_ld_excerpt=f"breadcrumb_leaf={candidate.breadcrumb_leaf!r}",
        )

    # 2. Brand must be in our universe of known brands. Empty brand or
    #    free-text brand we don't recognise → reject (rules-doc principle:
    #    "missing > wrong").
    if not _brand_is_known(candidate.brand):
        return None, RejectionRecord(
            url=candidate.url, gate="insert", reason_code="unknown_brand",
            json_ld_excerpt=f"brand={candidate.brand!r}",
        )

    # 3. Brand-in-slug consistency check (MASA-135 v3 hard reject).
    #    `_brand_mismatch_reason` returns "competing_brand_in_slug" if the
    #    slug advertises a different known brand than the product JSON-LD.
    slug = _slug_from_url(candidate.url)
    sitemap_norm = normalise(slug.replace("-", " "))
    mismatch = _brand_mismatch_reason(candidate.brand, sitemap_norm)
    if mismatch is not None:
        return None, RejectionRecord(
            url=candidate.url, gate="insert", reason_code=mismatch,
            json_ld_excerpt=f"brand={candidate.brand!r}, slug={slug!r}",
        )

    # 4. Image host must be whitelisted in web/next.config.js.
    image_host = _image_host(candidate.image_url)
    if image_host not in LIDL_IMAGE_HOST_WHITELIST:
        return None, RejectionRecord(
            url=candidate.url, gate="insert", reason_code="image_host_not_whitelisted",
            json_ld_excerpt=f"image_host={image_host!r}",
        )

    # 5. size_source_conflict — TODO when JSON-LD weight/volume extraction lands.

    # All gates passed. Compute confidence.
    confidence, features = _compute_confidence(candidate, hub)
    candidate.confidence_features = features

    return ImportProposal(
        proposed_name=candidate.name,
        proposed_brand=candidate.brand,
        proposed_category=hub,
        proposed_unit=candidate.size_text,
        image_url=candidate.image_url,
        json_ld_price=candidate.price,
        sitemap_url=candidate.url,
        confidence=confidence,
    ), None


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
