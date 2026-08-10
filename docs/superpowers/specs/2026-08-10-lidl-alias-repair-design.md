# Lidl alias repair tool — design

Date: 2026-08-10
Status: approved, not yet implemented

## Problem

64 of Lidl's 77 product aliases (83%) point at URLs that return HTTP 404. Lidl
rotates the SKU segment of its product URLs, so the stored `scraper_url` goes
stale while the product itself stays on sale. Every scrape run walks the same 64
dead URLs, uploads nothing, and the main batch only ever ingests the surviving
13 products.

`discover_lidl_aliases.py` cannot fix this. Its candidate query excludes any
product that already has a Lidl alias (`if pid in lidl_product_ids: continue`),
and these products all have one — broken, but present. Discovery finds products
with *no* alias; repair needs the opposite.

Retiring the aliases instead would lose real coverage: the products are still
listed, only the URL moved.

## Scope

A recurring tool, not a one-shot cleanup — Lidl will rotate SKUs again.

Out of scope: no CI workflow, no marking aliases unavailable, no Aldi. Aldi has
the same class of breakage (69 aliases redirecting off the PDP) and pass 2 would
transfer, but the sitemap source and URL pattern differ; that is separate work.

## Approach

Two passes, cheap-and-certain first.

Measured against the current sitemap (1383 URLs) on 2026-08-10:

| Case | Count | Path |
|---|---|---|
| Old slug still in sitemap under a new SKU | 14 / 64 | pass 1, `slug_exact` |
| No exact slug match | 50 / 64 | pass 2, `token_match` |

Examples of pure SKU rotation: `rice-krispies` 247198 → 10000280,
`italiamo-tiramisu` 10057356 → 10061479.

Both passes fetch HTML to check size, so the saving is not "no fetch" — it is
one fetch instead of several. Pass 1 knows the single URL to check. Pass 2 has
to fetch every candidate URL that clears the token threshold, each behind a
0.5–1.5s polite delay, to find out which one fits. Running slug-only would
leave 78% unrepaired. Hence both.

Each proposal records how it was resolved, because the evidence is not equally
strong: an identical slug under a new SKU is far better evidence than a 0.55
token overlap, and the reviewer needs to see which they are approving.

## Architecture

New `repair_lidl_aliases.py`, sibling to `discover_lidl_aliases.py`, importing
its engine rather than duplicating it: `fetch_lidl_sitemap_urls`, `normalise`,
`product_size`, `variant_tokens`, `token_score`, `apply_brand_mismatch_filter`,
`fetch_lidl_page`, `extract_size_from_html`, `_api_login`, `API_URL`.

Only two things are new: selecting broken aliases, and resolving product → URL.
The existing engine resolves URL → product (`resolve_url_group` answers "which
product wins this URL"), so pass 2 uses it as a per-URL validator rather than
calling it as-is.

## Data flow

1. Authenticate; `GET /api/product-aliases/store/Lidl?active_only=false`.
2. Select broken: `last_scrape_success is False` and `scraper_url` present,
   skipping aliases already flagged `is_unavailable`.
3. Re-verify liveness: fetch each selected URL. Only 404 and 410 count as
   broken. A 200 means the alias recovered and is left alone; any other status,
   a timeout, or a connection error is inconclusive and the alias is skipped
   with reason `liveness_check_inconclusive` rather than repaired. The DB flag
   can be stale or reflect a transient failure, and repairing a URL that still
   works would replace a good alias with a guess.
4. `GET /products/all-simple`; join on `product_id` for name, brand, unit.
5. **Pass 1 — `slug_exact`.** Extract the slug from the broken URL and look it
   up in the sitemap. Exactly one sitemap entry carrying that slug under a
   different SKU makes it a candidate; if several entries share the slug, pass 1
   declines and the product falls through to pass 2, which can tell them apart.
   When both the MM product and the page HTML yield a size they must match;
   when either lacks one, accept on slug identity and mark the proposal
   `slug_exact_unverified`. A size that is derivable on both sides but differs
   rejects the candidate, and the product falls through to pass 2.
6. **Pass 2 — `token_match`.** Score all sitemap entries with `token_score`,
   keeping those at or above the same 0.55 threshold `discover_lidl_aliases.py`
   uses, then apply the phase-1.5 brand-mismatch filter. Validate each surviving
   candidate URL by fetching its HTML and gating on size, with the same
   zero-tolerance rule as discovery: an MM product with no derivable size is
   rejected `unknown_mm_size` rather than guessed at. Exactly one survivor is
   accepted; zero is `no_match`; more than one is `ambiguous`.
7. Write `/tmp/lidl_repair_proposal_<timestamp>.json`.
8. `--apply` (opt-in): `PUT /api/product-aliases/{alias_id}` with a body of
   `{"scraper_url": ...}` only — `ProductAliasUpdate` has no required fields, so
   this is a partial update. Applies to `repairs` only.

## Output

```json
{
  "generated_at": "...",
  "sitemap_url_count": 1383,
  "broken_alias_count": 64,
  "verified_404_count": 64,
  "repairs": [
    {
      "alias_id": 770,
      "product_id": 1234,
      "product_name": "...",
      "old_url": "https://www.lidl.ie/p/potato-gratin/p10000681",
      "new_url": "https://www.lidl.ie/p/potato-gratin/p11219130",
      "method": "slug_exact",
      "product_size": "500g",
      "html_size": "500g",
      "score": null
    }
  ],
  "unmatched": [
    {"alias_id": 771, "product_name": "...", "old_url": "...", "reason": "no_match"}
  ],
  "counts_by_method": {"slug_exact": 0, "slug_exact_unverified": 0, "token_match": 0},
  "counts_by_reason": {
    "no_match": 0, "ambiguous": 0, "size_mismatch": 0, "unknown_mm_size": 0,
    "html_fetch_failed": 0, "liveness_check_inconclusive": 0
  }
}
```

`unmatched` entries are reported and never acted on. Deciding whether a product
is genuinely delisted stays a human call.

## Error handling

Sitemap or auth failure exits non-zero with no proposal written. A failed HTML
fetch for one candidate URL rejects that candidate and the run continues. Under
`--apply` a failed PUT is logged and the batch continues, but the script exits
non-zero if any write failed.

## Testing

`tests/test_repair_lidl_aliases.py`, `unittest`, no network — matching repo
convention. The decision logic lives in pure functions so it can be tested
directly:

- slug and SKU extraction from a Lidl product URL, including malformed input
- selecting broken aliases from an alias list: respects `last_scrape_success`,
  skips `is_unavailable`, skips rows with no `scraper_url`
- classifying a liveness-check status into broken / recovered / inconclusive
- pass-1 candidate lookup: one slug match accepts, several decline to pass 2
- pass-1 size rule across its three branches: both sizes present and equal,
  both present and different, either absent
- pass-2 outcome selection: one survivor accepts, zero is `no_match`, more than
  one is `ambiguous`

Network paths are exercised by running the script against the real API, not by
mocking them.

## Dependency

Requires the API-backed candidate query from commit `0560c77`
(`_api_login`, `API_URL` in `discover_lidl_aliases.py`), currently on
`feature/MASA-159-validation-tests` via PR #2 and not yet on main. This work is
branched on top of it.
