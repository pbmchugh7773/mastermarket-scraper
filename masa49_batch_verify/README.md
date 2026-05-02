# MASA-49 — Batch URL Verification

One-time read-only batch scrape of deactivated product-alias URLs, used by
MASA-43 Phase 3b to produce ground-truth data for safe product re-mapping.

## What it does

1. Reads `inactive_aliases.csv` (exported from prod with the SQL in MASA-49).
2. For each URL, dispatches to the right per-store parser.
3. Extracts: name, brand, size, price, image URL, category.
4. Writes one row per URL to `output/ground_truth.csv`.
5. Resumable: existing `alias_id`s are skipped on re-run.

## Running

```bash
cd masa49_batch_verify
python3 batch_verify.py --store tesco           # one store
python3 batch_verify.py --store all             # everything
python3 batch_verify.py --store supervalu --limit 5   # smoke test
```

### Backfilling bot-blocked rows (MASA-75)

When the existing `output/ground_truth.csv` has rows with
`scrape_status=bot_blocked` (40 rows from the 2026-04-20 run), pass
`--include-bot-blocked` to re-attempt them — the dispatcher will skip rows in
all other terminal statuses (`ok`, `404_removed`, `parse_error`, `error`):

```bash
python3 batch_verify.py --store aldi    --include-bot-blocked   # 15 rows
python3 batch_verify.py --store dunnes  --include-bot-blocked   # 24 rows
python3 batch_verify.py --store tesco   --include-bot-blocked   # 1 row
```

The default `requests`-based parsers will likely re-fail with `bot_blocked`
since Cloudflare's challenge is consistent — the flag is the *enabler* for
the subpass, but a separate Apify-actor-based parser path is required to
actually break through (see "Known gaps" below).

No production data is modified by this script — it's pure HTTP GET + parse.

## Output schema

`output/ground_truth.csv` — one row per processed URL:

| column | notes |
|--------|-------|
| `alias_id` | matches `product_aliases.id` |
| `store_name`, `scraper_url` | echo from input |
| `scrape_status` | `ok` / `404_removed` / `bot_blocked` / `parse_error` / `error` |
| `http_status` | raw HTTP response code |
| `scraped_name`, `scraped_brand`, `scraped_size`, `scraped_price`, `scraped_image_url`, `scraped_category` | ground-truth fields |
| `notes` | human-readable detail for non-ok rows |
| `timestamp` | ISO-8601 UTC |

### Status values

- **`ok`** — full record extracted; `scraped_name` + `scraped_price` present.
- **`404_removed`** — URL is definitively gone (HTTP 404 or a clear soft-404).
- **`bot_blocked`** — anti-bot intercepted us (HTTP 403 / Akamai / Cloudflare).
  URL may still be valid but needs a Selenium / Playwright / Cloudscraper pass.
- **`parse_error`** — HTTP 200 but required fields missing. See `notes`:
  - `js_required_for_price (name+size from WebPage node)` — SuperValu case:
    name + size extracted from `WebPage` JSON-LD, but price needs client-side
    JS rendering.
  - `no Product JSON-LD` — page loaded, no recognised structured data.
  - `missing price or name` — partial extraction only.
  - `suspected apify bug: price=<x>` — Dunnes rejected tiny price (known bug).
- **`error`** — network/transport failure. See `notes`.

## Input (2026-04-20 run)

Current prod state has 172 inactive aliases with `scraper_url` and
`last_scrape_success=true` (the task originally cited 678, but MASA-46 and
MASA-71 cleanup in the intervening days reduced the pool):

| store | count |
|-------|-------|
| Aldi | 15 |
| Dunnes Stores | 24 |
| SuperValu | 129 |
| Tesco | 4 |

## Result summary (2026-04-20 run)

| status | count | notes |
|--------|-------|-------|
| `ok` | 3 | Tesco only |
| `parse_error` | 129 | all SuperValu — partial (name+size), price needs JS |
| `bot_blocked` | 40 | 15 Aldi + 24 Dunnes (Cloudflare) + 1 Tesco (Akamai) |

132 of 172 URLs (77%) yielded at least a usable name for remap cross-referencing.

## Known gaps / next phase

- **40 bot-blocked rows (MASA-75)** — backfill subpass needs an
  Apify-actor-based parser path, NOT plain Selenium. The original spec
  said "reuse existing Selenium scrapers", but the production scrapers
  in this repo are Apify-based (`apify_dunnes_scraper.py`,
  `apify_tesco_scraper.py`, plus `apify-actors/{dunnes,tesco}-scraper/`
  for the actor source). Aldi has no standalone scraper here — the
  15 Aldi rows will need a new actor or a Cloudflare-bypass path.
  The `--include-bot-blocked` flag (this commit) prepares the dispatcher
  for that subpass; the Apify-runner parser modules are the next chunk
  of work.
- **SuperValu 129 `parse_error` rows** — not in MASA-75 scope. Name+size
  already extracted are sufficient for remapping; brand/image/price will
  be recovered during the next post-remap daily scraper run.
- **Category** — not emitted by the store JSON-LD in most cases. Can be
  derived from the breadcrumb trail if needed in a later pass.

## Dependencies

Only `requests` (stdlib otherwise). No optional extras needed — we
deliberately avoid `brotli` so the `Accept-Encoding` header is gzip/deflate
only and `requests` decompresses the body natively.
