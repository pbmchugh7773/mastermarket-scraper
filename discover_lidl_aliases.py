#!/usr/bin/env python3
"""
Lidl URL Discovery for MasterMarket — MASA-117 / parent MASA-114.

Sister to discover_aldi_aliases.py for Lidl.ie. Three Lidl-specific quirks:
  1. Sitemap is gzipped: https://www.lidl.ie/p/export/IE/en/product_sitemap.xml.gz
  2. Lidl URL slugs DO NOT include size/variant info (e.g. "hellmanns-mayonnaise")
     — unlike Aldi where slugs encode the variant — so collisions are common.
  3. To break collisions we fetch the Lidl product page and parse the
     JSON-LD `<script type="application/ld+json">` Product block to extract
     size from `name`/`description`/`weight`/`additionalProperty`.

This script NEVER writes to the DB or API. It emits a JSON proposal only;
VP Data reviews and creates aliases manually (or via a follow-up script).

Strategy
--------
1. Fetch + decompress Lidl sitemap → index URLs by normalised slug.
2. Get candidate products: branded products that have an Aldi alias but no
   Lidl alias (proxy for "Lidl-likely products we're missing").
   Two sources:
     --source api    (default) — uses MM API like discover_aldi_aliases.py.
     --source local  — runs scripts/query_prod.sh on the local machine.
3. Score by token overlap + brand-presence bonus + hard size gate.
4. For URLs that are claimed by ≥2 distinct MM products (collisions),
   fetch the Lidl page, extract size from JSON-LD, then re-gate the
   colliding candidates against the verified Lidl size. The winner (if any)
   gets promoted into the unambiguous proposal set.
5. Emit proposal JSON to cwd (CI uploads as artifact).

Usage
-----
    # Dry-mode discovery via MM API (the default)
    python discover_lidl_aliases.py

    # Local DB query (VP Engineering only — needs query_prod.sh)
    python discover_lidl_aliases.py --source local

    # Skip the JSON-LD verification step (faster, but loses collision recovery)
    python discover_lidl_aliases.py --no-verify-collisions

    # Tune match threshold or cap candidate volume
    python discover_lidl_aliases.py --threshold 0.5 --limit 500
"""

import argparse
import gzip
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

API_URL = os.getenv("API_URL", "https://api.mastermarketapp.com")
USERNAME = os.getenv("SCRAPER_USERNAME", "pricerIE@mastermarket.com")
PASSWORD = os.getenv("SCRAPER_PASSWORD", "pricerIE")

LIDL_SITEMAP_URL = "https://www.lidl.ie/p/export/IE/en/product_sitemap.xml.gz"
QUERY_PROD = os.getenv(
    "QUERY_PROD_PATH",
    "/home/pbmchugh7773/projects/MasterMarket/scripts/query_prod.sh",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            f'discover_lidl_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        ),
    ],
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Normalisation (kept identical to discover_aldi_aliases.py to preserve scoring parity)
# ----------------------------------------------------------------------------

SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|litre|liter|cl|oz|pk|pack)\b", re.I
)
MULTIPACK_RE = re.compile(r"\b(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|ml|kg)\b", re.I)
RATING_RE = re.compile(r"\d+\.?\d*\s*stars?\s*\(.*?\)", re.I)
FILLER_RE = re.compile(
    r"\b(the|a|an|of|in|with|and|&|for|from|style|original|classic)\b", re.I
)


def normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = MULTIPACK_RE.sub("", text)
    text = SIZE_RE.sub("", text)
    text = RATING_RE.sub("", text)
    text = FILLER_RE.sub("", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_size(text: str) -> Optional[str]:
    """Return canonical 'NNNunit' (e.g. '500g', '1.5l') or None."""
    if not text:
        return None
    m = SIZE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).lower()}{m.group(2).lower()}"


# ----------------------------------------------------------------------------
# Lidl sitemap fetch
# ----------------------------------------------------------------------------

def fetch_lidl_sitemap_urls() -> List[Dict]:
    """Fetch + decompress Lidl sitemap and index entries."""
    logger.info(f"Fetching Lidl sitemap: {LIDL_SITEMAP_URL}")
    resp = requests.get(LIDL_SITEMAP_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    raw = gzip.decompress(resp.content).decode("utf-8", errors="replace")
    raw_urls = re.findall(r"<loc>([^<]+)</loc>", raw)
    out: List[Dict] = []
    for u in raw_urls:
        m = re.search(r"/p/([^/]+)/p\d+", u)
        if not m:
            continue
        slug = m.group(1).replace("-", " ")
        out.append(
            {
                "url": u,
                "slug": slug,
                "norm": normalize(slug),
                "size": extract_size(slug),
            }
        )
    logger.info(f"  {len(out)} product URLs in sitemap (after slug filter)")
    return out


# ----------------------------------------------------------------------------
# Candidate products — two sources
# ----------------------------------------------------------------------------

CANDIDATE_SQL = """
SELECT p.id, p.name, COALESCE(p.brand,''), COALESCE(p.unit,'')
FROM products p
JOIN product_aliases a_aldi ON a_aldi.product_id = p.id AND a_aldi.store_name='Aldi'
LEFT JOIN product_aliases a_lidl ON a_lidl.product_id = p.id AND a_lidl.store_name='Lidl'
WHERE a_lidl.id IS NULL
  AND p.brand IS NOT NULL AND p.brand <> ''
  AND p.brand NOT IN ('Aldi','Lidl','Tesco','SuperValu','Dunnes','Dunnes Stores')
  AND p.brand NOT ILIKE '%aldi%'
  AND p.brand NOT ILIKE '%specially selected%'
  AND p.brand NOT ILIKE '%simply%'
ORDER BY p.id
{LIMIT};
"""


def query_candidates_local(limit: Optional[int]) -> List[Dict]:
    """VP-only path: use scripts/query_prod.sh against prod read replica."""
    if not Path(QUERY_PROD).exists():
        raise FileNotFoundError(
            f"query_prod.sh not found at {QUERY_PROD}. "
            "Use --source api or set QUERY_PROD_PATH."
        )
    sql = CANDIDATE_SQL.replace("{LIMIT}", f"LIMIT {limit}" if limit else "")
    logger.info("Querying candidate products via local query_prod.sh …")
    res = subprocess.run(
        [QUERY_PROD, sql], capture_output=True, text=True, check=True
    )
    products: List[Dict] = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        pid, name, brand, unit = int(parts[0]), parts[1], parts[2], parts[3]
        text = f"{name} {unit}".strip()
        products.append(
            {
                "id": pid,
                "name": name,
                "brand": brand,
                "unit": unit,
                "norm": normalize(f"{brand} {name}"),
                "size": extract_size(text),
            }
        )
    logger.info(f"  {len(products)} candidate products from local query")
    return products


def authenticate_api() -> requests.Session:
    """Authenticate with MasterMarket API (mirrors discover_aldi_aliases.py)."""
    s = requests.Session()
    resp = s.post(
        f"{API_URL}/auth/login",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("MM API auth response missing access_token")
    s.headers["Authorization"] = f"Bearer {token}"
    s.headers["User-Agent"] = HEADERS["User-Agent"]
    return s


def query_candidates_api(limit: Optional[int]) -> List[Dict]:
    """API path: get aliases, derive products that have Aldi but not Lidl."""
    logger.info("Authenticating with MM API …")
    s = authenticate_api()

    # Step 1: page through all aliases. We need to see Aldi+Lidl ownership per product.
    logger.info("Fetching aliases (paginated) …")
    page_size = 1000
    offset = 0
    aliases: List[Dict] = []
    while True:
        r = s.get(
            f"{API_URL}/api/aliases",
            params={"limit": page_size, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json() if isinstance(r.json(), list) else r.json().get("aliases", [])
        if not batch:
            break
        aliases.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset > 50000:  # safety belt
            logger.warning("Alias pagination cap hit at 50k — truncating.")
            break
    logger.info(f"  {len(aliases)} aliases total")

    by_product: Dict[int, set] = {}
    for a in aliases:
        pid = a.get("product_id")
        sn = (a.get("store_name") or "").strip()
        if pid is None or not sn:
            continue
        by_product.setdefault(pid, set()).add(sn)

    target_pids = [
        pid
        for pid, stores in by_product.items()
        if "Aldi" in stores and "Lidl" not in stores
    ]
    if limit:
        target_pids = target_pids[:limit]
    logger.info(f"  {len(target_pids)} products with Aldi alias but no Lidl alias")

    # Step 2: hydrate products
    products: List[Dict] = []
    excluded_brands = {
        "Aldi",
        "Lidl",
        "Tesco",
        "SuperValu",
        "Dunnes",
        "Dunnes Stores",
    }
    for pid in target_pids:
        try:
            r = s.get(f"{API_URL}/products/{pid}", timeout=15)
            if r.status_code != 200:
                continue
            p = r.json()
            brand = (p.get("brand") or "").strip()
            name = (p.get("name") or "").strip()
            unit = (p.get("unit") or "").strip()
            if not brand or brand in excluded_brands:
                continue
            blow = brand.lower()
            if (
                "aldi" in blow
                or "specially selected" in blow
                or "simply" in blow
            ):
                continue
            products.append(
                {
                    "id": pid,
                    "name": name,
                    "brand": brand,
                    "unit": unit,
                    "norm": normalize(f"{brand} {name}"),
                    "size": extract_size(f"{name} {unit}"),
                }
            )
        except requests.RequestException as e:
            logger.debug(f"Skipped product {pid}: {e}")
            continue
        # Be polite to the API
        if len(products) % 100 == 0 and len(products) > 0:
            time.sleep(0.2)
    logger.info(f"  {len(products)} branded candidates after hydration")
    return products


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def score(product: Dict, sitemap_entry: Dict) -> float:
    """Token-overlap with size gating, identical scoring to /tmp prototype."""
    p_tokens = set(product["norm"].split())
    s_tokens = set(sitemap_entry["norm"].split())
    if not p_tokens or not s_tokens:
        return 0.0
    overlap = len(p_tokens & s_tokens)
    union = len(p_tokens | s_tokens)
    jaccard = overlap / union if union else 0.0

    brand_norm = normalize(product["brand"])
    brand_bonus = 0.15 if brand_norm and brand_norm in sitemap_entry["norm"] else 0.0

    if (
        product.get("size")
        and sitemap_entry.get("size")
        and product["size"] != sitemap_entry["size"]
    ):
        return 0.0

    if overlap < 2:
        return 0.0

    return min(jaccard + brand_bonus, 1.0)


# ----------------------------------------------------------------------------
# JSON-LD verification — the new piece in MASA-117
# ----------------------------------------------------------------------------

LD_SIZE_PROPERTY_KEYS = {
    "weight",
    "size",
    "netcontent",
    "net content",
    "package size",
    "content",
    "quantity",
}


def _coerce_value(v) -> str:
    """Schema.org Quantity may be string '500 g', dict {value, unitText}, etc."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        val = v.get("value") or v.get("name") or ""
        unit = v.get("unitText") or v.get("unitCode") or ""
        return f"{val} {unit}".strip()
    return str(v)


def parse_jsonld_size(html: str) -> Optional[str]:
    """Extract canonical size token (e.g. '500g') from a Lidl product page HTML.

    Tries, in order:
      1. Schema.org Product `weight`
      2. Schema.org Product `additionalProperty` (key matches LD_SIZE_PROPERTY_KEYS)
      3. Regex over the Product `name`
      4. Regex over the Product `description`
    Returns None if no size can be extracted with confidence.
    """
    blocks = re.findall(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue

        # Schema.org sometimes wraps Product in @graph
        candidates = []
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            candidates.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)

        for c in candidates:
            if not isinstance(c, dict):
                continue
            t = c.get("@type")
            if isinstance(t, list):
                is_product = any(str(x).lower() == "product" for x in t)
            else:
                is_product = str(t).lower() == "product"
            if not is_product:
                continue

            # 1) explicit weight
            if c.get("weight"):
                size = extract_size(_coerce_value(c["weight"]))
                if size:
                    return size

            # 2) additionalProperty
            ap = c.get("additionalProperty")
            if isinstance(ap, list):
                for prop in ap:
                    if not isinstance(prop, dict):
                        continue
                    pname = (prop.get("name") or prop.get("propertyID") or "").lower()
                    if any(key in pname for key in LD_SIZE_PROPERTY_KEYS):
                        size = extract_size(_coerce_value(prop.get("value")))
                        if size:
                            return size

            # 3) name
            for field in ("name", "description"):
                size = extract_size(c.get(field, ""))
                if size:
                    return size

    return None


def verify_lidl_url(url: str, session: requests.Session) -> Tuple[Optional[str], bool]:
    """Returns (size_token_or_None, page_was_reachable_bool)."""
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.debug(f"  page {url} HTTP {r.status_code}")
            return (None, False)
        return (parse_jsonld_size(r.text), True)
    except requests.RequestException as e:
        logger.debug(f"  page {url} fetch error: {e}")
        return (None, False)


def resolve_collisions(
    by_url: Dict[str, List[Dict]],
    products_by_id: Dict[int, Dict],
    session: requests.Session,
    max_calls: int,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """For each colliding URL, fetch JSON-LD size and re-gate candidates.

    Returns (recovered_proposals, still_unresolved, verified_collision_meta).
      - recovered_proposals: collisions where exactly one MM candidate matches
        the verified Lidl size — promoted to the unambiguous set.
      - still_unresolved: collisions where 0 or ≥2 candidates still match
        (or the page was unreachable / no size found).
      - verified_collision_meta: per-URL detail for the proposal JSON audit log.
    """
    recovered: List[Dict] = []
    unresolved: List[Dict] = []
    audit: List[Dict] = []
    calls_made = 0

    for url, group in by_url.items():
        if len(group) <= 1:
            continue
        if calls_made >= max_calls:
            audit.append(
                {"url": url, "status": "skipped_call_cap", "candidates": len(group)}
            )
            unresolved.extend(group)
            continue

        verified_size, reachable = verify_lidl_url(url, session)
        calls_made += 1

        record = {
            "url": url,
            "verified_size": verified_size,
            "reachable": reachable,
            "candidate_count": len(group),
        }

        if not reachable or not verified_size:
            record["status"] = "no_size_extracted"
            audit.append(record)
            unresolved.extend(group)
            continue

        # Re-gate: only candidates whose own size matches the verified Lidl size pass.
        passing = [
            p
            for p in group
            if products_by_id[p["product_id"]].get("size") == verified_size
        ]
        if len(passing) == 1:
            winner = passing[0]
            winner = {**winner, "lidl_verified_size": verified_size}
            recovered.append(winner)
            record["status"] = "resolved_to_one"
            record["winner_product_id"] = winner["product_id"]
        else:
            record["status"] = (
                "still_ambiguous"
                if passing
                else "no_candidate_matches_verified_size"
            )
            record["passing_count"] = len(passing)
            unresolved.extend(group)

        audit.append(record)
        time.sleep(0.5)  # be nice to Lidl

    return recovered, unresolved, audit


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Discover Lidl URLs for MM products.")
    ap.add_argument(
        "--source",
        choices=["api", "local"],
        default="api",
        help="Where to read candidate products from. 'local' uses query_prod.sh.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap candidate volume.")
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.55,
        help="Minimum match score to keep a proposal (0..1).",
    )
    ap.add_argument(
        "--no-verify-collisions",
        action="store_true",
        help="Skip JSON-LD verification for collision URLs.",
    )
    ap.add_argument(
        "--max-verify-calls",
        type=int,
        default=200,
        help="Cap on Lidl page fetches during collision verification.",
    )
    ap.add_argument(
        "--output-dir",
        default=".",
        help="Where to write the proposal JSON. Defaults to cwd.",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc)

    # 1. Sitemap
    try:
        sitemap = fetch_lidl_sitemap_urls()
    except Exception as e:
        logger.error(f"Sitemap fetch failed: {e}")
        return 2

    # 2. Candidates
    try:
        if args.source == "local":
            products = query_candidates_local(args.limit)
        else:
            products = query_candidates_api(args.limit)
    except Exception as e:
        logger.error(f"Candidate query failed ({args.source}): {e}")
        return 3

    if not products:
        logger.warning("No candidate products — nothing to match. Exiting clean.")

    products_by_id = {p["id"]: p for p in products}

    # 3. Score
    proposals: List[Dict] = []
    for prod in products:
        best: Tuple[float, Optional[Dict]] = (0.0, None)
        for entry in sitemap:
            s = score(prod, entry)
            if s > best[0]:
                best = (s, entry)
        if best[1] and best[0] >= args.threshold:
            proposals.append(
                {
                    "product_id": prod["id"],
                    "product_name": prod["name"],
                    "product_brand": prod["brand"],
                    "product_size": prod.get("size"),
                    "lidl_url": best[1]["url"],
                    "lidl_slug": best[1]["slug"],
                    "lidl_size": best[1].get("size"),
                    "confidence": round(best[0], 3),
                }
            )
    proposals.sort(key=lambda p: -p["confidence"])
    logger.info(f"Raw matches above threshold {args.threshold}: {len(proposals)}")

    # 4. Detect collisions
    by_url: Dict[str, List[Dict]] = {}
    for p in proposals:
        by_url.setdefault(p["lidl_url"], []).append(p)
    unambiguous = [g[0] for g in by_url.values() if len(g) == 1]
    collisions = [g for g in by_url.values() if len(g) > 1]
    logger.info(
        f"Unambiguous: {len(unambiguous)} | "
        f"colliding URLs: {len(collisions)} (covering {sum(len(g) for g in collisions)} candidates)"
    )

    # 5. JSON-LD verification (the MASA-117 addition)
    verification_audit: List[Dict] = []
    recovered: List[Dict] = []
    rejected_collisions: List[Dict] = []
    if collisions and not args.no_verify_collisions:
        logger.info(
            f"Verifying {len(collisions)} collision URLs via JSON-LD …"
        )
        verify_session = requests.Session()
        recovered, rejected_collisions, verification_audit = resolve_collisions(
            by_url, products_by_id, verify_session, args.max_verify_calls
        )
        logger.info(
            f"  recovered (size disambiguated): {len(recovered)} | "
            f"still ambiguous: {len(rejected_collisions)}"
        )
    else:
        rejected_collisions = [p for g in collisions for p in g]
        if collisions:
            logger.info("Skipping collision verification (per --no-verify-collisions)")

    final_proposals = sorted(
        unambiguous + recovered, key=lambda p: -p["confidence"]
    )

    # 6. Emit JSON
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"lidl_discovery_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(
        json.dumps(
            {
                "generated_at": started_at.isoformat(),
                "source": args.source,
                "sitemap_url_count": len(sitemap),
                "candidate_product_count": len(products),
                "threshold": args.threshold,
                "raw_match_count": len(proposals),
                "unambiguous_count": len(unambiguous),
                "collision_url_count": len(collisions),
                "collision_resolved_count": len(recovered),
                "collision_unresolved_count": len(rejected_collisions),
                "proposals": final_proposals,
                "rejected_collisions": rejected_collisions,
                "verification_audit": verification_audit,
            },
            indent=2,
        )
    )
    logger.info(f"Wrote {out_path}")
    print(f"\nWrote {out_path}")
    print(f"  unambiguous (slug-only): {len(unambiguous)}")
    print(f"  recovered (JSON-LD size verified): {len(recovered)}")
    print(f"  total promotable proposals: {len(final_proposals)}")
    print(f"  unresolved collisions: {len(rejected_collisions)}")
    if final_proposals[:15]:
        print("\nTop proposals:")
        for p in final_proposals[:15]:
            tag = " (verified)" if "lidl_verified_size" in p else ""
            print(
                f"  [{p['confidence']:.2f}] {p['product_brand']} :: "
                f"{p['product_name'][:60]}{tag}  →  {p['lidl_url']}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
