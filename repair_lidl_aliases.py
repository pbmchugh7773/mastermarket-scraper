#!/usr/bin/env python3
"""
Lidl broken-alias repair.

Most of Lidl's aliases point at URLs that now return HTTP 404: Lidl rotates the
SKU segment of its product URLs while the product stays on sale.
discover_lidl_aliases.py cannot fix these — its candidate query excludes any
product that already has a Lidl alias, and these have one. Retiring them would
lose real coverage, since the products are still listed.

Two passes:
  * pass 1 `slug_exact`  — the old slug is still in the sitemap under a new SKU.
  * pass 2 `token_match` — token overlap + the HTML size gate, same engine and
    threshold discovery uses.

Proposal-only by default. `--apply` updates scraper_url for accepted repairs
and never touches unmatched aliases.

Scale drifts week to week as Lidl rotates its range, so treat any figure here
as a dated observation rather than a fixture: on 2026-08-10 the design run saw
64 of 77 aliases broken, and on 2026-08-16 a live proposal-only run saw a
1431-URL sitemap and 11 pass-1 slug hits.

See docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from discover_lidl_aliases import (
    USER_AGENT,
    HTTP_TIMEOUT,
    API_TIMEOUT,
    normalise,
    product_size,
    variant_tokens,
    token_score,
    apply_brand_mismatch_filter,
    API_URL,
    _api_login,
    fetch_lidl_sitemap_urls,
    fetch_lidl_page,
    extract_size_from_html,
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


def landed_on_pdp(requested_url, final_url):
    """
    True when a redirect chain ending at `final_url` is still on a Lidl PDP.

    Delegates to simple_local_to_prod._validate_pdp_redirect so the Lidl PDP
    regex has exactly one definition in the repo (PDP_PATTERNS['lidl']). That
    module is imported lazily and not at module scope: importing it runs
    logging.basicConfig() — which hijacks the root logger — and drags in
    selenium, webdriver_manager and bs4 for a helper that is three lines of
    regex. No final URL to compare against means no redirect evidence either
    way, which the helper already treats as "proceed".
    """
    from simple_local_to_prod import _validate_pdp_redirect

    ok, _reason = _validate_pdp_redirect("lidl", requested_url, final_url)
    return ok


def classify_liveness(status, requested_url=None, final_url=None):
    """
    'broken' | 'recovered' | 'inconclusive' for a liveness-check result.

    Only 404/410 justify replacing a URL. A 200 normally means the alias fixed
    itself and must be left alone — but only if it is still a 200 for a product
    page. Lidl is known to bounce expired SKUs onto a category landing page,
    which answers 200 for a URL that no longer sells anything; treating that as
    'recovered' would make a dead fleet look self-healed and suppress every
    repair. A 200 that redirected off the PDP pattern is therefore inconclusive.
    Anything else — 403, 500, a timeout surfacing as None — is inconclusive too:
    repairing on it would swap a good URL for a guess.
    """
    if status in BROKEN_STATUSES:
        return "broken"
    if status == 200:
        if not landed_on_pdp(requested_url, final_url):
            return "inconclusive"
        return "recovered"
    return "inconclusive"


def check_url_liveness(url):
    """
    (status, final_url) for `url`, or (None, None) if the request failed.

    The final URL is returned alongside the status because the status alone
    cannot distinguish a live PDP from a redirect to a category page — see
    classify_liveness.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code, resp.url
    except requests.RequestException:
        return None, None


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

    Only called once the candidate page has actually been retrieved, so
    'unverified' means "the HTML is in hand but no size could be read out of
    it, or the MM product has none to compare against" — never "the page could
    not be fetched", which repair_one handles before it gets here. An identical
    slug under a new SKU is strong evidence on its own, so an underivable size
    does not veto the repair; it downgrades it to slug_exact_unverified. Two
    sizes that are both known and disagree do veto it: that is a different
    format sharing a slug.
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


def fetch_lidl_aliases(token):
    """Every Lidl alias, active or not — a parked alias can still be broken."""
    resp = requests.get(
        f"{API_URL}/api/product-aliases/store/Lidl",
        params={"active_only": "false"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_products_by_id():
    """/products/all-simple keyed by product id. Public endpoint, no auth."""
    resp = requests.get(
        f"{API_URL}/products/all-simple",
        headers={"User-Agent": USER_AGENT},
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()
    return {row["id"]: row for row in resp.json() if row.get("id") is not None}


def build_repair_record(alias, product, entry, method, html_size, score):
    return {
        "alias_id": alias["id"],
        "product_id": product["id"],
        "product_name": product["name"],
        "product_brand": product["brand"],
        "old_url": alias["scraper_url"],
        "new_url": entry["url"],
        "method": method,
        "product_size": product["size"],
        "html_size": html_size,
        "score": round(score, 3) if score is not None else None,
    }


def build_unmatched_record(alias, product, reason):
    return {
        "alias_id": alias["id"],
        "product_id": product["id"],
        "product_name": product["name"],
        "product_brand": product["brand"],
        "old_url": alias["scraper_url"],
        "reason": reason,
    }


def repair_one(alias, product, sitemap, sitemap_by_slug, fetch_log):
    """
    (repair_record | None, unmatched_record | None) for a single broken alias.

    Pass 1 first: an identical slug under a new SKU is far stronger evidence
    than a 0.55 token overlap, and it needs one HTML fetch instead of one per
    candidate. Two things make pass 1 fall through to pass 2 rather than fail
    outright:
      * a size mismatch — the slug may genuinely belong to a different format
        now, and pass 2 can tell the formats apart;
      * a failed fetch — fetch_lidl_page returns None both for a transport
        failure and for a candidate that itself 404s, so an unretrieved page is
        no evidence at all. Accepting it would propose a URL we never saw, and
        under --apply would overwrite one dead URL with another and count it a
        success. Pass 2 rejects the same condition as html_fetch_failed, so the
        weaker-evidence path must not be the more permissive one.
    """
    slug, old_sku = parse_lidl_url(alias["scraper_url"])

    candidate = find_slug_candidate(slug, old_sku, sitemap_by_slug)
    if candidate is not None:
        html = fetch_lidl_page(candidate["url"], fetch_log)
        if html is not None:
            html_size = extract_size_from_html(html)
            verdict = decide_slug_size(product["size"], html_size)
            if verdict == "match":
                return build_repair_record(
                    alias, product, candidate, "slug_exact", html_size, None
                ), None
            if verdict == "unverified":
                return build_repair_record(
                    alias, product, candidate, "slug_exact_unverified", html_size, None
                ), None

    # Pass 2 accepts only on an MM-size/HTML-size equality, so a product with no
    # derivable size cannot survive it no matter what the sitemap offers. Bail
    # before the loop: inside it, this cost one 0.5-1.5s live fetch per candidate
    # to reach a foregone rejection, and left the reported reason to a Counter
    # tie-break between unknown_mm_size and no_html_size.
    if product["size"] is None:
        return None, build_unmatched_record(alias, product, "unknown_mm_size")

    survivors = []
    rejections = []
    scored = select_token_candidates(product, sitemap, REPAIR_THRESHOLD)
    for entry in scored:
        html = fetch_lidl_page(entry["url"], fetch_log)
        if html is None:
            rejections.append("html_fetch_failed")
            continue
        html_size = extract_size_from_html(html)
        if not html_size:
            rejections.append("no_html_size")
            continue
        if product["size"].lower() == html_size.lower():
            survivors.append((entry, html_size))
        else:
            rejections.append("size_mismatch")

    chosen, reason = choose_token_outcome([e for e, _ in survivors], rejections)
    if chosen is not None:
        html_size = next(hs for e, hs in survivors if e["url"] == chosen["url"])
        score = token_score(product, chosen)
        return build_repair_record(
            alias, product, chosen, "token_match", html_size, score
        ), None
    return None, build_unmatched_record(alias, product, reason)


def _put_scraper_url(alias_id, payload, token):
    resp = requests.put(
        f"{API_URL}/api/product-aliases/{alias_id}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=API_TIMEOUT,
    )
    resp.raise_for_status()


def apply_repairs(repairs, token, put_fn=None):
    """
    Write each accepted repair's new URL back. Returns (applied, failed).

    ProductAliasUpdate has no required fields, so the body carries scraper_url
    alone — no risk of clobbering alias_name or scrape_frequency_hours by
    round-tripping a whole object. One failure is logged and the batch
    continues; main() exits non-zero if any failed.
    """
    put_fn = put_fn or _put_scraper_url
    applied = failed = 0
    for repair in repairs:
        try:
            put_fn(repair["alias_id"], {"scraper_url": repair["new_url"]}, token)
            applied += 1
        except Exception as exc:  # noqa: BLE001 — one bad alias must not abort
            failed += 1
            print(
                f"  ! alias {repair['alias_id']} update failed: {exc}",
                file=sys.stderr,
            )
    return applied, failed


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Repair Lidl aliases whose scraper_url now 404s."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the proposed URLs back via PUT /api/product-aliases/{id}. "
            "Without this the run is proposal-only. Unmatched aliases are "
            "never touched either way."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N broken aliases. For smoke-testing a change.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    fetch_log = {}

    print("Fetching Lidl sitemap …", file=sys.stderr)
    sitemap = fetch_lidl_sitemap_urls()
    sitemap_by_slug = index_sitemap_by_slug(sitemap)
    print(f"  {len(sitemap)} product URLs", file=sys.stderr)

    token = _api_login()
    aliases = fetch_lidl_aliases(token)
    broken = select_broken_aliases(aliases)
    print(f"  {len(broken)} aliases flagged broken", file=sys.stderr)

    products_by_id = fetch_all_products_by_id()

    repairs, unmatched = [], []
    verified = 0
    for alias in broken[: args.limit]:
        row = products_by_id.get(alias["product_id"])
        if row is None:
            unmatched.append(
                {
                    "alias_id": alias["id"],
                    "product_id": alias["product_id"],
                    "product_name": alias.get("alias_name", ""),
                    "product_brand": "",
                    "old_url": alias["scraper_url"],
                    "reason": "product_not_found",
                }
            )
            continue
        product = build_match_product(row)

        status, final_url = check_url_liveness(alias["scraper_url"])
        state = classify_liveness(status, alias["scraper_url"], final_url)
        if state != "broken":
            reason = (
                "alias_recovered"
                if state == "recovered"
                else "liveness_check_inconclusive"
            )
            unmatched.append(build_unmatched_record(alias, product, reason))
            continue
        verified += 1

        repair, miss = repair_one(
            alias, product, sitemap, sitemap_by_slug, fetch_log
        )
        if repair is not None:
            repairs.append(repair)
        else:
            unmatched.append(miss)

    applied, failed = (0, 0)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sitemap_url_count": len(sitemap),
        "broken_alias_count": len(broken),
        "verified_404_count": verified,
        "repairs": repairs,
        "unmatched": unmatched,
        "counts_by_method": _count_by(repairs, "method"),
        "counts_by_reason": _count_by(unmatched, "reason"),
        "applied": applied,
        "apply_failures": failed,
    }
    out_path = Path(
        f"/tmp/lidl_repair_proposal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    )
    # Persist BEFORE mutating production. Every old_url exists only in memory
    # until this lands, so a crash mid-batch — or a failing write after the PUTs
    # — would leave rewritten aliases with nothing to roll back from. The second
    # write below only decorates the same file with the outcome counts.
    out_path.write_text(json.dumps(out, indent=2))

    if args.apply:
        applied, failed = apply_repairs(repairs, token)
        out["applied"] = applied
        out["apply_failures"] = failed
        out_path.write_text(json.dumps(out, indent=2))

    print(f"\nWrote {out_path}")
    print(f"  broken aliases:     {len(broken)}")
    print(f"  verified 404:       {verified}")
    print(f"  repairs proposed:   {len(repairs)}  {out['counts_by_method']}")
    print(f"  unmatched:          {len(unmatched)}  {out['counts_by_reason']}")
    if args.apply:
        print(f"  applied:            {applied} (failures={failed})")
    for r in repairs[:15]:
        print(f"  [{r['method']}] {r['product_name'][:50]}")
        print(f"      {r['old_url']}  →  {r['new_url']}")
    return 1 if failed else 0


def _count_by(records, key):
    return dict(sorted(Counter(r[key] for r in records).items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":
    sys.exit(main())
