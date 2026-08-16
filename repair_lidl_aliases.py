#!/usr/bin/env python3
"""
Lidl broken-alias repair.

64 of Lidl's 77 aliases point at URLs that now return HTTP 404: Lidl rotates
the SKU segment of its product URLs while the product stays on sale.
discover_lidl_aliases.py cannot fix these — its candidate query excludes any
product that already has a Lidl alias, and these have one. Retiring them would
lose real coverage, since the products are still listed.

Two passes:
  * pass 1 `slug_exact`  — the old slug is still in the sitemap under a new SKU.
  * pass 2 `token_match` — token overlap + the HTML size gate, same engine and
    threshold discovery uses.

Proposal-only by default. `--apply` updates scraper_url for accepted repairs
and never touches unmatched aliases.

See docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md
"""
import re
from collections import Counter

import requests

from discover_lidl_aliases import (
    USER_AGENT,
    HTTP_TIMEOUT,
    normalise,
    product_size,
    variant_tokens,
    token_score,
    apply_brand_mismatch_filter,
)

BROKEN_STATUSES = (404, 410)
REPAIR_THRESHOLD = 0.55

LIDL_PRODUCT_URL_RE = re.compile(r"/p/([^/?#]+)/p(\d+)")


def parse_lidl_url(url):
    """('potato-gratin', '10000681') for a Lidl PDP URL, else None."""
    if not url:
        return None
    m = LIDL_PRODUCT_URL_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2)


def select_broken_aliases(aliases):
    """
    Aliases worth attempting to repair.

    `last_scrape_success is False` is the breakage signal — None means the
    alias has never been scraped, which is not evidence of a dead URL. Aliases
    already flagged is_unavailable have been triaged by a human, and ones whose
    URL will not parse cannot be slug-matched.
    """
    out = []
    for alias in aliases:
        if alias.get("last_scrape_success") is not False:
            continue
        if alias.get("is_unavailable"):
            continue
        url = alias.get("scraper_url")
        if not url or parse_lidl_url(url) is None:
            continue
        out.append(alias)
    out.sort(key=lambda a: a["id"])
    return out


def classify_liveness(status):
    """
    'broken' | 'recovered' | 'inconclusive' for a liveness-check status.

    Only 404/410 justify replacing a URL. A 200 means the alias fixed itself
    and must be left alone. Anything else — 403, 500, a timeout surfacing as
    None — is inconclusive: repairing on it would swap a good URL for a guess.
    """
    if status in BROKEN_STATUSES:
        return "broken"
    if status == 200:
        return "recovered"
    return "inconclusive"


def check_url_liveness(url):
    """HTTP status for `url`, or None if the request could not complete."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code
    except requests.RequestException:
        return None


def index_sitemap_by_slug(sitemap):
    """Group sitemap entries by their URL slug, so pass 1 is a dict lookup."""
    idx = {}
    for entry in sitemap:
        parsed = parse_lidl_url(entry["url"])
        if parsed is None:
            continue
        idx.setdefault(parsed[0], []).append(entry)
    return idx


def find_slug_candidate(slug, old_sku, sitemap_by_slug):
    """
    The sitemap entry that carries `slug` under a SKU other than `old_sku`.

    Returns None when the slug is gone, when the only hit is the dead SKU
    itself, or when several entries share the slug — that last case is
    ambiguous and pass 2 resolves it with the size gate instead of guessing.
    """
    hits = [e for e in sitemap_by_slug.get(slug, []) if e["sku"] != old_sku]
    if len(hits) != 1:
        return None
    return hits[0]


def decide_slug_size(mm_size, html_size):
    """
    'match' | 'mismatch' | 'unverified' for the pass-1 size check.

    An identical slug under a new SKU is strong evidence on its own, so a size
    we cannot derive on either side does not veto the repair — it downgrades it
    to slug_exact_unverified. Two sizes that are both known and disagree do
    veto it: that is a different format sharing a slug.
    """
    if mm_size is None or html_size is None:
        return "unverified"
    return "match" if mm_size.lower() == html_size.lower() else "mismatch"


def build_match_product(row):
    """
    Reshape a /products/all-simple row into what the matching engine expects.

    Same key set select_candidate_products() produces in discover_lidl_aliases,
    so token_score() and apply_brand_mismatch_filter() accept it unchanged.
    """
    name = (row.get("name") or "").strip()
    brand = (row.get("brand") or "").strip()
    unit = (row.get("unit") or "").strip()
    return {
        "id": row.get("id"),
        "name": name,
        "brand": brand,
        "unit": unit,
        "norm": normalise(f"{brand} {name}"),
        "size": product_size(name, unit),
        "variant": variant_tokens(name),
    }


def select_token_candidates(product, sitemap, threshold):
    """
    Sitemap entries scoring >= threshold for `product`, after the phase-1.5
    brand-mismatch hard reject.

    Discovery groups by URL because it asks "which product wins this URL".
    Repair knows the product and wants its URLs, so it builds a one-candidate
    group per URL and reuses the same filter.
    """
    by_url = {}
    entry_by_url = {}
    for entry in sitemap:
        score = token_score(product, entry)
        if score >= threshold:
            by_url[entry["url"]] = [{**product, "score": score}]
            entry_by_url[entry["url"]] = entry
    kept, _rejections = apply_brand_mismatch_filter(by_url)
    return [entry_by_url[url] for url in kept]


def choose_token_outcome(survivors, rejections):
    """
    (chosen_entry, reason) for one product's pass-2 candidates.

    Exactly one survivor is the repair. Several means the size gate could not
    separate them. None means we report why the closest candidates failed, so
    the reviewer can tell "wrong size on file" from "nothing resembled it".
    """
    if len(survivors) == 1:
        return survivors[0], None
    if len(survivors) > 1:
        return None, "ambiguous"
    if rejections:
        return None, Counter(rejections).most_common(1)[0][0]
    return None, "no_match"
