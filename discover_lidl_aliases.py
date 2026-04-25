#!/usr/bin/env python3
"""
Lidl URL Discovery for MasterMarket — v2 (MASA-119).

Changes vs v1:
  * Per-candidate HTML fetch with size extraction from visible spans
    (Lidl JSON-LD does NOT expose pack size; rendered HTML does — see MASA-116).
  * Mandatory size gate: a Lidl proposal MUST carry a non-null lidl_size.
  * Collision disambiguation: when several MM products map to one Lidl URL,
    fetch HTML once and keep only candidates whose size matches AND whose
    variant tokens are compatible with the page title.
  * 24h disk cache at ~/.cache/mastermarket/lidl_html/<sku>.html for re-runs.
  * 0.5–1.5s polite delay between LIVE fetches (skipped on cache hits).
  * Product-side size derivation: try the name first; fall back to `unit`
    only if name yields nothing AND `unit` is not "1 portion (200ml)" garbage
    (portion|serving|cup|glass|slice|bowl in unit → treat as unknown).

Output: /tmp/lidl_proposal_<timestamp>.json — proposal-only, NO DB writes.
VP Data signs off the next batch.
"""
import gzip
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LIDL_SITEMAP_URL = "https://www.lidl.ie/p/export/IE/en/product_sitemap.xml.gz"
QUERY_PROD = "/home/pbmchugh7773/projects/MasterMarket/scripts/query_prod.sh"
USER_AGENT = "Mozilla/5.0 (compatible; MasterMarket-Discovery/0.2)"
CACHE_DIR = Path.home() / ".cache" / "mastermarket" / "lidl_html"
CACHE_TTL_SECONDS = 24 * 3600
LIVE_FETCH_MIN_DELAY = 0.5
LIVE_FETCH_MAX_DELAY = 1.5
HTTP_TIMEOUT = 15

# Same normalisation rules as discover_aldi_aliases.py — keep behaviour parallel.
SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|litre|liter|cl|oz|pk|pack)\b",
    re.I,
)
MULTIPACK_RE = re.compile(r"\b(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|ml|kg)\b", re.I)
RATING_RE = re.compile(r"\d+\.?\d*\s*stars?\s*\(.*?\)", re.I)
FILLER = re.compile(
    r"\b(the|a|an|of|in|with|and|&|for|from|style|original|classic)\b", re.I
)

# Detect "garbage" unit fields on the MM side (rule #5 from the issue spec).
PORTION_GUARD_RE = re.compile(
    r"\b(portion|serving|cup|glass|slice|bowl)\b", re.I
)

# HTML size patterns — ordered: multipack first (most specific), then weight/volume, then pack-count.
HTML_MULTIPACK_RE = re.compile(
    r"\b(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|ml|kg|l|cl)\b", re.I
)
HTML_WEIGHT_VOLUME_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(kg|g|ml|cl|l|ltr|litre|liter)\b", re.I
)
HTML_PACK_RE = re.compile(r"\b(\d+)\s*(pack|pk)\b", re.I)

# Variant tokens we use to gate ambiguous matches against the Lidl page title.
VARIANT_TOKENS = (
    "light",
    "real",
    "regular",
    "diet",
    "zero",
    "decaf",
    "organic",
    "salted",
    "unsalted",
    "spreadable",
    "low",
    "fat",
    "skim",
    "skimmed",
    "whole",
    "smooth",
    "crunchy",
)


# --------------------------------------------------------------------------- #
# Normalisation + product-side size derivation
# --------------------------------------------------------------------------- #
def normalise(text: str) -> str:
    text = text.lower().strip()
    text = MULTIPACK_RE.sub("", text)
    text = SIZE_RE.sub("", text)
    text = RATING_RE.sub("", text)
    text = FILLER.sub("", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_size_from_text(text: str):
    """Generic helper used for sitemap slugs (legacy parity)."""
    if not text:
        return None
    m_multi = MULTIPACK_RE.search(text)
    if m_multi:
        return f"{m_multi.group(1)}x{m_multi.group(2).lower()}{m_multi.group(3).lower()}"
    m = SIZE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).lower()}{m.group(2).lower()}"


def product_size(name: str, unit: str):
    """
    Derive a usable size for an MM product.

    Priority:
      1. From the product `name` (most reliable — curated by data team).
      2. From `unit`, but only if it does NOT contain portion/serving/cup/etc.
         (those are brewed-tea / serving descriptions, not pack sizes).
    Returns None if nothing trustworthy is available.
    """
    s = extract_size_from_text(name or "")
    if s:
        return s
    if unit and not PORTION_GUARD_RE.search(unit):
        return extract_size_from_text(unit)
    return None


def variant_tokens(text: str) -> set:
    if not text:
        return set()
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    return tokens & set(VARIANT_TOKENS)


# --------------------------------------------------------------------------- #
# Sitemap + DB
# --------------------------------------------------------------------------- #
def fetch_lidl_sitemap_urls():
    req = urllib.request.Request(LIDL_SITEMAP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        gz = resp.read()
    raw = gzip.decompress(gz).decode("utf-8", errors="replace")
    urls = re.findall(r"<loc>([^<]+)</loc>", raw)
    out = []
    for u in urls:
        m = re.search(r"/p/([^/]+)/p(\d+)", u)
        if not m:
            continue
        slug = m.group(1).replace("-", " ")
        sku = m.group(2)
        out.append(
            {
                "url": u,
                "slug": slug,
                "sku": sku,
                "norm": normalise(slug),
                "slug_size": extract_size_from_text(slug),
            }
        )
    return out


def query_candidate_products():
    """Branded products that have an Aldi alias but no Lidl alias."""
    sql = """
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
ORDER BY p.id;
"""
    res = subprocess.run([QUERY_PROD, sql], capture_output=True, text=True, check=True)
    products = []
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        pid, name, brand, unit = int(parts[0]), parts[1], parts[2], parts[3]
        size = product_size(name, unit)
        products.append(
            {
                "id": pid,
                "name": name,
                "brand": brand,
                "unit": unit,
                "norm": normalise(f"{brand} {name}"),
                "size": size,
                "variant": variant_tokens(name),
            }
        )
    return products


# --------------------------------------------------------------------------- #
# HTTP layer with 24h disk cache + polite delay between LIVE fetches
# --------------------------------------------------------------------------- #
def _cache_path_for(url: str) -> Path:
    sku_match = re.search(r"/p(\d+)\b", url)
    key = sku_match.group(1) if sku_match else re.sub(r"[^a-z0-9]+", "_", url.lower())
    return CACHE_DIR / f"{key}.html"


def fetch_lidl_page(url: str, fetch_log: dict) -> str | None:
    """Returns the HTML body, or None on failure. Caches to disk for 24h."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for(url)
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            fetch_log["cache_hits"] = fetch_log.get("cache_hits", 0) + 1
            return cache_path.read_text(encoding="utf-8", errors="replace")

    fetch_log["live_fetch_attempts"] = fetch_log.get("live_fetch_attempts", 0) + 1
    delay = random.uniform(LIVE_FETCH_MIN_DELAY, LIVE_FETCH_MAX_DELAY)
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        cache_path.write_text(body, encoding="utf-8")
        return body
    except Exception as e:
        fetch_log["live_fetch_failures"] = fetch_log.get("live_fetch_failures", 0) + 1
        fetch_log.setdefault("failure_samples", []).append(
            {"url": url, "error": f"{type(e).__name__}: {e}"}
        )
        return None


# --------------------------------------------------------------------------- #
# HTML parsing — extract size + variant tokens from a Lidl page
# --------------------------------------------------------------------------- #
def _visible_spans(html: str) -> list[str]:
    return [
        s.strip()
        for s in re.findall(r"<span[^>]*>([^<]{1,200})</span>", html)
        if s.strip()
    ]


def extract_size_from_html(html: str) -> str | None:
    """
    Try in order: multipack → weight/volume → pack-count.
    Look first inside visible <span> content; fall back to whole HTML
    only if nothing trips on spans (avoids picking up sizes from script
    blocks, UTM params, etc.).
    """
    spans = _visible_spans(html)
    span_text = " | ".join(spans)
    for source in (span_text, html):
        m = HTML_MULTIPACK_RE.search(source)
        if m:
            return f"{m.group(1)}x{m.group(2).lower()}{m.group(3).lower()}"
        m = HTML_WEIGHT_VOLUME_RE.search(source)
        if m:
            unit = m.group(2).lower()
            unit = "l" if unit in ("ltr", "litre", "liter") else unit
            return f"{m.group(1).lower()}{unit}"
        m = HTML_PACK_RE.search(source)
        if m:
            return f"{m.group(1)}pack"
    return None


def extract_page_text_signals(html: str) -> dict:
    """
    Return tokens we can use to disambiguate collisions. We pull from:
      - <title>...</title>
      - JSON-LD `name`
      - The first <h1> if present (some Lidl skins render it)
    """
    bits = []
    m = re.search(r"<title[^>]*>([^<]{1,300})</title>", html)
    if m:
        bits.append(m.group(1))
    m = re.search(r"<h1[^>]*>([^<]{1,300})</h1>", html)
    if m:
        bits.append(m.group(1))
    for ld in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            obj = json.loads(ld.group(1))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("@type") in ("Product", "ProductGroup"):
            n = obj.get("name")
            if isinstance(n, str):
                bits.append(n)
            d = obj.get("description")
            if isinstance(d, str):
                bits.append(d)
    text = " ".join(bits)
    return {"text": text, "variant": variant_tokens(text)}


# --------------------------------------------------------------------------- #
# Token-overlap scoring (phase 1 candidate gating)
# --------------------------------------------------------------------------- #
def token_score(product, sitemap_entry):
    """0..1 token overlap score. Used only to pick PHASE-1 candidates."""
    p_tokens = set(product["norm"].split())
    s_tokens = set(sitemap_entry["norm"].split())
    if not p_tokens or not s_tokens:
        return 0.0
    overlap = len(p_tokens & s_tokens)
    union = len(p_tokens | s_tokens)
    jaccard = overlap / union if union else 0.0
    brand_norm = normalise(product["brand"])
    brand_bonus = 0.15 if brand_norm and brand_norm in sitemap_entry["norm"] else 0.0
    if overlap < 2:
        return 0.0
    return min(jaccard + brand_bonus, 1.0)


# --------------------------------------------------------------------------- #
# Phase 2: HTML-gated resolution per URL group
# --------------------------------------------------------------------------- #
def resolve_url_group(url: str, candidates: list, fetch_log: dict, threshold: float):
    """
    Given a Lidl URL and the list of MM products that scored >= threshold for it,
    fetch the HTML, extract size + variant tokens, and return:
      (accepted_proposal_dict | None, rejection_records: list)

    Rules (zero-tolerance):
      - HTML must yield a size. If none → reject ALL candidates with reason 'no_html_size'.
      - For each candidate: MM size must equal HTML size. If MM size is None → reject 'unknown_mm_size'.
      - After size filter, if multiple still match, gate by variant_tokens
        being a subset of HTML's variant tokens. If still >1 → reject 'unresolved_variant'.
      - Exactly one survivor → accept.
    """
    rejections = []
    html = fetch_lidl_page(url, fetch_log)
    if html is None:
        for c in candidates:
            rejections.append(
                {**_proposal_record(c, url, None, c["score"]), "reason": "html_fetch_failed"}
            )
        return None, rejections

    html_size = extract_size_from_html(html)
    if not html_size:
        fetch_log["no_size_in_html"] = fetch_log.get("no_size_in_html", 0) + 1
        for c in candidates:
            rejections.append(
                {**_proposal_record(c, url, None, c["score"]), "reason": "no_html_size"}
            )
        return None, rejections

    page_signals = extract_page_text_signals(html)

    # Step 1: size filter.
    survivors = []
    for c in candidates:
        if c["size"] is None:
            rejections.append(
                {**_proposal_record(c, url, html_size, c["score"]), "reason": "unknown_mm_size"}
            )
            continue
        if c["size"].lower() == html_size.lower():
            survivors.append(c)
        else:
            rejections.append(
                {**_proposal_record(c, url, html_size, c["score"]), "reason": "size_mismatch"}
            )

    if not survivors:
        return None, rejections

    if len(survivors) == 1:
        return _proposal_record(survivors[0], url, html_size, survivors[0]["score"]), rejections

    # Step 2: variant-token gate. Each survivor must declare a non-empty variant
    # set that is a subset of the page's variant tokens (e.g. MM "Light" matches
    # page "Light", but MM "Real" against a "Light"-only page is rejected).
    page_variants = page_signals["variant"]
    final = []
    for c in survivors:
        if not c["variant"]:
            # No variant signal on MM side — keep, but treat as ambiguous if peer has variant info.
            final.append((c, "no_variant"))
        elif c["variant"].issubset(page_variants):
            final.append((c, "variant_match"))
        else:
            rejections.append(
                {
                    **_proposal_record(c, url, html_size, c["score"]),
                    "reason": "variant_mismatch",
                    "page_variants": sorted(page_variants),
                }
            )

    if len(final) == 1:
        c, _ = final[0]
        return _proposal_record(c, url, html_size, c["score"]), rejections

    # Still >1 — unresolved.
    for c, _ in final:
        rejections.append(
            {
                **_proposal_record(c, url, html_size, c["score"]),
                "reason": "unresolved_variant",
                "page_variants": sorted(page_variants),
            }
        )
    return None, rejections


def _proposal_record(product, url, lidl_size, score):
    return {
        "product_id": product["id"],
        "product_name": product["name"],
        "product_brand": product["brand"],
        "product_size": product["size"],
        "product_variant": sorted(product["variant"]),
        "lidl_url": url,
        "lidl_size": lidl_size,
        "confidence": round(score, 3),
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    print(f"[{datetime.utcnow().isoformat()}] Fetching Lidl sitemap …", file=sys.stderr)
    sitemap = fetch_lidl_sitemap_urls()
    print(f"  {len(sitemap)} product URLs in sitemap", file=sys.stderr)

    print(f"[{datetime.utcnow().isoformat()}] Querying candidate products …", file=sys.stderr)
    products = query_candidate_products()
    print(
        f"  {len(products)} branded products with Aldi alias but no Lidl alias",
        file=sys.stderr,
    )
    products_with_known_size = sum(1 for p in products if p["size"])
    print(
        f"  {products_with_known_size} of those have a derivable size "
        f"(after portion-guard)",
        file=sys.stderr,
    )

    threshold = 0.55
    # Phase 1 — token overlap candidates per URL.
    by_url: dict[str, list] = {}
    for prod in products:
        for entry in sitemap:
            s = token_score(prod, entry)
            if s >= threshold:
                by_url.setdefault(entry["url"], []).append({**prod, "score": s})

    # Phase 2 — HTML-gated resolution per URL group.
    fetch_log: dict = {}
    proposals: list = []
    rejected: list = []
    for url, group in by_url.items():
        accepted, rejs = resolve_url_group(url, group, fetch_log, threshold)
        if accepted:
            proposals.append(accepted)
        rejected.extend(rejs)

    proposals.sort(key=lambda p: -p["confidence"])

    # Final invariant: NO proposal may have null lidl_size (acceptance criterion).
    bad = [p for p in proposals if not p.get("lidl_size")]
    if bad:
        # Defensive: should be impossible given resolve_url_group, but assert anyway.
        for p in bad:
            rejected.append({**p, "reason": "null_lidl_size_post_resolve"})
        proposals = [p for p in proposals if p.get("lidl_size")]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sitemap_url_count": len(sitemap),
        "candidate_product_count": len(products),
        "candidates_with_known_size": products_with_known_size,
        "threshold": threshold,
        "phase1_url_groups": len(by_url),
        "html_fetch": {
            "live_attempts": fetch_log.get("live_fetch_attempts", 0),
            "live_failures": fetch_log.get("live_fetch_failures", 0),
            "cache_hits": fetch_log.get("cache_hits", 0),
            "no_size_in_html": fetch_log.get("no_size_in_html", 0),
            "failure_samples": fetch_log.get("failure_samples", [])[:5],
        },
        "unambiguous_count": len(proposals),
        "rejected_count": len(rejected),
        "rejection_breakdown": _summarise_reasons(rejected),
        "proposals": proposals,
        "rejected": rejected,
    }
    out_path = Path(
        f"/tmp/lidl_proposal_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")
    print(f"  phase1 url groups: {len(by_url)}")
    print(f"  live fetches: {fetch_log.get('live_fetch_attempts', 0)} "
          f"(failures={fetch_log.get('live_fetch_failures', 0)}, "
          f"cache_hits={fetch_log.get('cache_hits', 0)})")
    print(f"  no_size_in_html: {fetch_log.get('no_size_in_html', 0)}")
    print(f"  unambiguous: {len(proposals)}")
    print(f"  rejected: {len(rejected)}")
    print("  rejection breakdown:")
    for reason, n in _summarise_reasons(rejected).items():
        print(f"    {reason}: {n}")
    print("\nUnambiguous proposals (top 15):")
    for p in proposals[:15]:
        print(
            f"  [{p['confidence']:.2f}] {p['product_brand']} :: "
            f"{p['product_name'][:60]}  →  {p['lidl_url']}  "
            f"(MM={p['product_size']}, Lidl={p['lidl_size']})"
        )


def _summarise_reasons(records: list) -> dict:
    out: dict = {}
    for r in records:
        reason = r.get("reason", "unknown")
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    main()
