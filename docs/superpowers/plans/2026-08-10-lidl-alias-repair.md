# Lidl Alias Repair Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `repair_lidl_aliases.py`, a recurring tool that finds Lidl aliases whose `scraper_url` now 404s and proposes the replacement URL, writing to production only under an explicit `--apply`.

**Architecture:** A sibling script to `discover_lidl_aliases.py` that imports its matching engine rather than duplicating it. Two passes: pass 1 resolves aliases whose old slug still exists in the sitemap under a new SKU (14 of the current 64); pass 2 runs the remaining 50 through token overlap plus the HTML size gate. All decision logic lives in pure functions so it is testable without network.

**Tech Stack:** Python 3.10, `requests`, `unittest`. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md`
- Default run is proposal-only. Writes happen only under `--apply`, only for entries in `repairs`, never for `unmatched`.
- Token overlap threshold is `0.55` — the same value `discover_lidl_aliases.py` uses.
- Only HTTP 404 and 410 count as broken. 200 means recovered, anything else is inconclusive.
- Tests use `unittest` and must not touch the network. Run with `python3 -m unittest`.
- Lidl only. Do not add Aldi handling.
- This branch (`feature/lidl-alias-repair`) is stacked on `feature/MASA-159-validation-tests`; `_api_login` and `API_URL` come from commit `0560c77` and are not on main.

## File Structure

- Create `repair_lidl_aliases.py` — the whole tool. One file, mirroring how `discover_lidl_aliases.py` is organised (constants, pure helpers, API layer, passes, `main`).
- Create `tests/test_repair_lidl_aliases.py` — all tests.
- No changes to `discover_lidl_aliases.py`. It is imported, not modified.

Imported from `discover_lidl_aliases`: `API_URL`, `USER_AGENT`, `HTTP_TIMEOUT`, `_api_login`, `fetch_lidl_sitemap_urls`, `fetch_lidl_page`, `extract_size_from_html`, `product_size`, `normalise`, `variant_tokens`, `token_score`, `apply_brand_mismatch_filter`.

---

### Task 1: URL parsing and broken-alias selection

**Files:**
- Create: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_lidl_url(url: str) -> tuple[str, str] | None` returning `(slug, sku)`; `select_broken_aliases(aliases: list[dict]) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
"""
Tests for repair_lidl_aliases — the Lidl broken-alias repair tool.

Pure-Python, no network. See
docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md
"""
from __future__ import annotations

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from repair_lidl_aliases import (  # noqa: E402
    parse_lidl_url,
    select_broken_aliases,
)


def _alias(alias_id, **kw):
    """An alias row shaped like /api/product-aliases/store/Lidl returns it."""
    row = {
        "id": alias_id,
        "product_id": 1000 + alias_id,
        "store_name": "Lidl",
        "alias_name": f"product {alias_id}",
        "scraper_url": f"https://www.lidl.ie/p/slug-{alias_id}/p{alias_id}0000",
        "last_scrape_success": False,
        "is_unavailable": False,
    }
    row.update(kw)
    return row


class ParseLidlUrlTests(unittest.TestCase):
    def test_extracts_slug_and_sku(self):
        self.assertEqual(
            parse_lidl_url("https://www.lidl.ie/p/potato-gratin/p10000681"),
            ("potato-gratin", "10000681"),
        )

    def test_ignores_query_string_and_trailing_slash(self):
        self.assertEqual(
            parse_lidl_url("https://www.lidl.ie/p/rice-krispies/p247198/?x=1"),
            ("rice-krispies", "247198"),
        )

    def test_returns_none_for_non_product_url(self):
        self.assertIsNone(parse_lidl_url("https://www.lidl.ie/c/some-category"))

    def test_returns_none_for_empty_or_null(self):
        self.assertIsNone(parse_lidl_url(""))
        self.assertIsNone(parse_lidl_url(None))


class SelectBrokenAliasesTests(unittest.TestCase):
    def test_keeps_alias_whose_last_scrape_failed(self):
        out = select_broken_aliases([_alias(1)])
        self.assertEqual([a["id"] for a in out], [1])

    def test_drops_alias_whose_last_scrape_succeeded(self):
        out = select_broken_aliases([_alias(1, last_scrape_success=True)])
        self.assertEqual(out, [])

    def test_drops_alias_never_scraped(self):
        """last_scrape_success=None means no evidence of breakage."""
        out = select_broken_aliases([_alias(1, last_scrape_success=None)])
        self.assertEqual(out, [])

    def test_drops_alias_already_flagged_unavailable(self):
        out = select_broken_aliases([_alias(1, is_unavailable=True)])
        self.assertEqual(out, [])

    def test_drops_alias_with_no_scraper_url(self):
        for missing in (None, ""):
            out = select_broken_aliases([_alias(1, scraper_url=missing)])
            self.assertEqual(out, [])

    def test_drops_alias_with_unparseable_url(self):
        out = select_broken_aliases([_alias(1, scraper_url="https://www.lidl.ie/c/x")])
        self.assertEqual(out, [])

    def test_result_is_ordered_by_alias_id(self):
        out = select_broken_aliases([_alias(30), _alias(10), _alias(20)])
        self.assertEqual([a["id"] for a in out], [10, 20, 30])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'repair_lidl_aliases'`

- [ ] **Step 3: Write minimal implementation**

Create `repair_lidl_aliases.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): parse Lidl PDP URLs and select broken aliases"
```

---

### Task 2: Liveness classification

**Files:**
- Modify: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: `BROKEN_STATUSES` from Task 1.
- Produces: `classify_liveness(status: int | None) -> str` returning `"broken"`, `"recovered"` or `"inconclusive"`; `check_url_liveness(url: str) -> int | None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair_lidl_aliases.py` (extend the import block with `classify_liveness`):

```python
class ClassifyLivenessTests(unittest.TestCase):
    def test_404_and_410_are_broken(self):
        self.assertEqual(classify_liveness(404), "broken")
        self.assertEqual(classify_liveness(410), "broken")

    def test_200_is_recovered(self):
        self.assertEqual(classify_liveness(200), "recovered")

    def test_server_error_is_inconclusive(self):
        """A 500 or a 403 is not evidence the product is gone."""
        self.assertEqual(classify_liveness(500), "inconclusive")
        self.assertEqual(classify_liveness(403), "inconclusive")

    def test_none_is_inconclusive(self):
        """None means timeout or connection error — never repair on that."""
        self.assertEqual(classify_liveness(None), "inconclusive")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ImportError: cannot import name 'classify_liveness'`

- [ ] **Step 3: Write minimal implementation**

Add to `repair_lidl_aliases.py` (and add `import requests` plus `from discover_lidl_aliases import USER_AGENT, HTTP_TIMEOUT` at the top):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 15 tests

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): classify liveness-check results"
```

---

### Task 3: Pass 1 — slug-exact matching

**Files:**
- Modify: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: `parse_lidl_url` from Task 1.
- Produces: `index_sitemap_by_slug(sitemap: list[dict]) -> dict[str, list[dict]]`; `find_slug_candidate(slug: str, old_sku: str, sitemap_by_slug: dict) -> dict | None`; `decide_slug_size(mm_size: str | None, html_size: str | None) -> str` returning `"match"`, `"mismatch"` or `"unverified"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair_lidl_aliases.py` (extend imports with `index_sitemap_by_slug`, `find_slug_candidate`, `decide_slug_size`):

```python
def _entry(slug, sku):
    """A sitemap entry shaped like fetch_lidl_sitemap_urls() returns it."""
    return {
        "url": f"https://www.lidl.ie/p/{slug}/p{sku}",
        "slug": slug.replace("-", " "),
        "sku": sku,
        "norm": slug.replace("-", " "),
        "slug_size": None,
    }


class FindSlugCandidateTests(unittest.TestCase):
    def test_finds_same_slug_under_new_sku(self):
        idx = index_sitemap_by_slug([_entry("rice-krispies", "10000280")])
        hit = find_slug_candidate("rice-krispies", "247198", idx)
        self.assertEqual(hit["sku"], "10000280")

    def test_returns_none_when_slug_absent(self):
        idx = index_sitemap_by_slug([_entry("rice-krispies", "10000280")])
        self.assertIsNone(find_slug_candidate("potato-gratin", "10000681", idx))

    def test_returns_none_when_only_hit_is_the_same_dead_sku(self):
        """Same slug AND same SKU is the URL we already know is dead."""
        idx = index_sitemap_by_slug([_entry("rice-krispies", "247198")])
        self.assertIsNone(find_slug_candidate("rice-krispies", "247198", idx))

    def test_declines_when_several_entries_share_the_slug(self):
        """Ambiguous — pass 2 can tell them apart, pass 1 cannot."""
        idx = index_sitemap_by_slug(
            [_entry("italiamo-tiramisu", "1"), _entry("italiamo-tiramisu", "2")]
        )
        self.assertIsNone(find_slug_candidate("italiamo-tiramisu", "999", idx))


class DecideSlugSizeTests(unittest.TestCase):
    def test_both_sizes_present_and_equal_is_match(self):
        self.assertEqual(decide_slug_size("500g", "500g"), "match")

    def test_comparison_is_case_insensitive(self):
        self.assertEqual(decide_slug_size("500G", "500g"), "match")

    def test_both_sizes_present_and_different_is_mismatch(self):
        self.assertEqual(decide_slug_size("500g", "750g"), "mismatch")

    def test_missing_mm_size_is_unverified(self):
        self.assertEqual(decide_slug_size(None, "500g"), "unverified")

    def test_missing_html_size_is_unverified(self):
        self.assertEqual(decide_slug_size("500g", None), "unverified")

    def test_both_missing_is_unverified(self):
        self.assertEqual(decide_slug_size(None, None), "unverified")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ImportError: cannot import name 'index_sitemap_by_slug'`

- [ ] **Step 3: Write minimal implementation**

Add to `repair_lidl_aliases.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 25 tests

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): pass 1 slug-exact candidate lookup and size rule"
```

---

### Task 4: Pass 2 — token matching and outcome selection

**Files:**
- Modify: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: `REPAIR_THRESHOLD` from Task 1.
- Produces: `build_match_product(row: dict) -> dict` with keys `id, name, brand, unit, norm, size, variant`; `select_token_candidates(product: dict, sitemap: list[dict], threshold: float) -> list[dict]`; `choose_token_outcome(survivors: list[dict], rejections: list[str]) -> tuple[dict | None, str | None]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair_lidl_aliases.py` (extend imports with `build_match_product`, `select_token_candidates`, `choose_token_outcome`, `REPAIR_THRESHOLD`):

```python
class BuildMatchProductTests(unittest.TestCase):
    def test_emits_keys_the_matching_engine_consumes(self):
        p = build_match_product(
            {"id": 7, "name": "Barista Oat Milk 1L", "brand": "Vemondo", "unit": "l"}
        )
        self.assertEqual(
            set(p), {"id", "name", "brand", "unit", "norm", "size", "variant"}
        )
        self.assertEqual(p["id"], 7)

    def test_null_brand_name_and_unit_become_empty_strings(self):
        p = build_match_product({"id": 7, "name": None, "brand": None, "unit": None})
        self.assertEqual(p["name"], "")
        self.assertEqual(p["brand"], "")
        self.assertEqual(p["unit"], "")


class SelectTokenCandidatesTests(unittest.TestCase):
    def test_keeps_entries_at_or_above_threshold(self):
        product = build_match_product(
            {"id": 1, "name": "Parmigiano Reggiano DOP", "brand": "Italiamo", "unit": "g"}
        )
        sitemap = [
            _entry("italiamo-parmigiano-reggiano-dop", "111"),
            _entry("dulano-chicken-nuggets", "222"),
        ]
        urls = [e["url"] for e in select_token_candidates(product, sitemap, REPAIR_THRESHOLD)]
        self.assertIn("https://www.lidl.ie/p/italiamo-parmigiano-reggiano-dop/p111", urls)
        self.assertNotIn("https://www.lidl.ie/p/dulano-chicken-nuggets/p222", urls)

    def test_drops_candidate_whose_slug_advertises_a_competing_brand(self):
        """Phase-1.5 brand hard reject — the Vemondo/Alpro near-miss."""
        product = build_match_product(
            {"id": 1, "name": "Barista Oat Milk", "brand": "Vemondo", "unit": "l"}
        )
        sitemap = [_entry("alpro-alpro-barista-oat-milk", "333")]
        self.assertEqual(select_token_candidates(product, sitemap, REPAIR_THRESHOLD), [])

    def test_returns_empty_when_nothing_scores(self):
        product = build_match_product(
            {"id": 1, "name": "Completely Unrelated Item", "brand": "Milbona", "unit": "g"}
        )
        sitemap = [_entry("dulano-chicken-nuggets", "222")]
        self.assertEqual(select_token_candidates(product, sitemap, REPAIR_THRESHOLD), [])


class ChooseTokenOutcomeTests(unittest.TestCase):
    def test_single_survivor_is_accepted(self):
        entry = _entry("rice-krispies", "1")
        chosen, reason = choose_token_outcome([entry], [])
        self.assertEqual(chosen["sku"], "1")
        self.assertIsNone(reason)

    def test_several_survivors_is_ambiguous(self):
        chosen, reason = choose_token_outcome(
            [_entry("a", "1"), _entry("b", "2")], []
        )
        self.assertIsNone(chosen)
        self.assertEqual(reason, "ambiguous")

    def test_no_survivors_reports_the_commonest_rejection(self):
        chosen, reason = choose_token_outcome(
            [], ["size_mismatch", "size_mismatch", "no_html_size"]
        )
        self.assertIsNone(chosen)
        self.assertEqual(reason, "size_mismatch")

    def test_no_survivors_and_no_rejections_is_no_match(self):
        chosen, reason = choose_token_outcome([], [])
        self.assertIsNone(chosen)
        self.assertEqual(reason, "no_match")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ImportError: cannot import name 'build_match_product'`

- [ ] **Step 3: Write minimal implementation**

Add to `repair_lidl_aliases.py` (extend the `discover_lidl_aliases` import with `normalise`, `product_size`, `variant_tokens`, `token_score`, `apply_brand_mismatch_filter`, and add `from collections import Counter`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 34 tests

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): pass 2 token candidates and outcome selection"
```

---

### Task 5: API layer, record shapes and orchestration

**Files:**
- Modify: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: `fetch_lidl_aliases(token: str) -> list[dict]`; `fetch_all_products_by_id() -> dict[int, dict]`; `build_repair_record(alias, product, entry, method, html_size, score) -> dict`; `build_unmatched_record(alias, product, reason) -> dict`; `repair_one(alias, product, sitemap, sitemap_by_slug, fetch_log) -> tuple[dict | None, dict | None]`; `main(argv=None) -> int`.

**Note on ordering:** `main` calls `apply_repairs`, which Task 6 adds. Python
resolves that name at call time, so the tests and `--help` work after this
task, but a full `--apply` run is only possible once Task 6 lands. Do not
reorder the tasks to "fix" this — Task 6 has its own test cycle.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair_lidl_aliases.py` (extend imports with `build_repair_record`, `build_unmatched_record`):

```python
class RecordShapeTests(unittest.TestCase):
    def test_repair_record_carries_both_urls_and_the_method(self):
        alias = _alias(770, scraper_url="https://www.lidl.ie/p/rice-krispies/p247198")
        product = build_match_product(
            {"id": 55, "name": "Rice Krispies 340g", "brand": "Kellogg's", "unit": "g"}
        )
        entry = _entry("rice-krispies", "10000280")
        rec = build_repair_record(alias, product, entry, "slug_exact", "340g", None)
        self.assertEqual(rec["alias_id"], 770)
        self.assertEqual(rec["product_id"], 55)
        self.assertEqual(rec["old_url"], "https://www.lidl.ie/p/rice-krispies/p247198")
        self.assertEqual(rec["new_url"], "https://www.lidl.ie/p/rice-krispies/p10000280")
        self.assertEqual(rec["method"], "slug_exact")
        self.assertEqual(rec["html_size"], "340g")
        self.assertIsNone(rec["score"])

    def test_repair_record_rounds_a_token_score(self):
        alias = _alias(771)
        product = build_match_product({"id": 56, "name": "X", "brand": "Y", "unit": "g"})
        rec = build_repair_record(
            alias, product, _entry("x", "1"), "token_match", "500g", 0.61234
        )
        self.assertEqual(rec["score"], 0.612)

    def test_unmatched_record_carries_the_reason_and_no_new_url(self):
        alias = _alias(772)
        product = build_match_product({"id": 57, "name": "X", "brand": "Y", "unit": "g"})
        rec = build_unmatched_record(alias, product, "no_match")
        self.assertEqual(rec["reason"], "no_match")
        self.assertNotIn("new_url", rec)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ImportError: cannot import name 'build_repair_record'`

- [ ] **Step 3: Write minimal implementation**

Add to `repair_lidl_aliases.py` (extend imports with `argparse`, `json`, `sys`, `from datetime import datetime`, `from pathlib import Path`, and from `discover_lidl_aliases`: `API_URL`, `_api_login`, `fetch_lidl_sitemap_urls`, `fetch_lidl_page`, `extract_size_from_html`):

```python
API_TIMEOUT = 60


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
    candidate. A pass-1 size mismatch falls through to pass 2 rather than
    failing outright — the slug may genuinely belong to a different format now.
    """
    slug, old_sku = parse_lidl_url(alias["scraper_url"])

    candidate = find_slug_candidate(slug, old_sku, sitemap_by_slug)
    if candidate is not None:
        html = fetch_lidl_page(candidate["url"], fetch_log)
        html_size = extract_size_from_html(html) if html else None
        verdict = decide_slug_size(product["size"], html_size)
        if verdict == "match":
            return build_repair_record(
                alias, product, candidate, "slug_exact", html_size, None
            ), None
        if verdict == "unverified":
            return build_repair_record(
                alias, product, candidate, "slug_exact_unverified", html_size, None
            ), None

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
        if product["size"] is None:
            rejections.append("unknown_mm_size")
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
```

And the orchestration:

```python
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

        state = classify_liveness(check_url_liveness(alias["scraper_url"]))
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
    if args.apply:
        applied, failed = apply_repairs(repairs, token)

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
```

Add `timezone` to the datetime import: `from datetime import datetime, timezone`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 37 tests

Then check the module imports cleanly and the CLI wires up:

Run: `python3 repair_lidl_aliases.py --help`
Expected: usage text listing `--apply` and `--limit`

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): API layer, record shapes and orchestration"
```

---

### Task 6: `--apply` write path

**Files:**
- Modify: `repair_lidl_aliases.py`
- Test: `tests/test_repair_lidl_aliases.py`

**Interfaces:**
- Consumes: repair records from Task 5.
- Produces: `apply_repairs(repairs: list[dict], token: str, put_fn=None) -> tuple[int, int]` returning `(applied, failed)`.

`put_fn` is an injection seam so the batching and failure-tolerance can be tested without network. It defaults to the real HTTP call.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair_lidl_aliases.py` (extend imports with `apply_repairs`):

```python
class ApplyRepairsTests(unittest.TestCase):
    def _repairs(self, n):
        return [
            {"alias_id": i, "new_url": f"https://www.lidl.ie/p/x/p{i}"}
            for i in range(1, n + 1)
        ]

    def test_sends_one_put_per_repair_with_only_scraper_url(self):
        calls = []

        def fake_put(alias_id, payload, token):
            calls.append((alias_id, payload, token))

        applied, failed = apply_repairs(self._repairs(2), "tok", put_fn=fake_put)
        self.assertEqual((applied, failed), (2, 0))
        self.assertEqual([c[0] for c in calls], [1, 2])
        self.assertEqual(calls[0][1], {"scraper_url": "https://www.lidl.ie/p/x/p1"})
        self.assertEqual(calls[0][2], "tok")

    def test_one_failure_does_not_stop_the_batch(self):
        def fake_put(alias_id, payload, token):
            if alias_id == 2:
                raise RuntimeError("boom")

        applied, failed = apply_repairs(self._repairs(3), "tok", put_fn=fake_put)
        self.assertEqual((applied, failed), (2, 1))

    def test_empty_batch_is_a_no_op(self):
        applied, failed = apply_repairs([], "tok", put_fn=lambda *a: None)
        self.assertEqual((applied, failed), (0, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: FAIL with `ImportError: cannot import name 'apply_repairs'`

- [ ] **Step 3: Write minimal implementation**

Add to `repair_lidl_aliases.py`, above `main`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_repair_lidl_aliases -v`
Expected: PASS, 40 tests

Then confirm the whole suite is still green:

Run: `python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add repair_lidl_aliases.py tests/test_repair_lidl_aliases.py
git commit -m "feat(repair): --apply write path with per-alias failure tolerance"
```

---

### Task 7: Live proposal-only run

**Files:**
- None. This task verifies the tool against the real API and sitemap.

**Interfaces:**
- Consumes: the finished script.
- Produces: a proposal JSON to inspect before anyone runs `--apply`.

- [ ] **Step 1: Run against production, proposal-only**

Requires `API_URL`, `SCRAPER_USERNAME`, `SCRAPER_PASSWORD` in the environment.

Run: `python3 repair_lidl_aliases.py --limit 5`
Expected: writes `/tmp/lidl_repair_proposal_*.json`, prints a summary, exits 0. No writes to production — `--apply` was not passed.

- [ ] **Step 2: Check the proposals are sane**

Run: `python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('/tmp/lidl_repair_proposal_*.json'))[-1])); print(json.dumps(d['repairs'], indent=1)[:2000]); print(d['counts_by_method'], d['counts_by_reason'])"`

Expected: every `new_url` differs from its `old_url`, and `slug_exact` entries share the slug with their `old_url`.

- [ ] **Step 3: Full run**

Run: `python3 repair_lidl_aliases.py`
Expected: `broken aliases` around 64, `verified 404` close to it, and roughly 14 `slug_exact` repairs based on the 2026-08-10 sitemap measurement. Treat a large divergence as a signal to investigate, not to apply.

- [ ] **Step 4: Report before applying**

Do NOT run `--apply` as part of this plan. Summarise the proposal for the user — counts by method, counts by reason, and a sample — and let them decide.

---

## Notes for the implementer

- `fetch_lidl_page` caches to `~/.cache/mastermarket/lidl_html` for 24h and sleeps 0.5–1.5s between live fetches. A full run takes minutes; that is expected, not a hang.
- `product_size` applies a portion guard, so it returns None more often than you would guess. That is why `decide_slug_size` has an `unverified` branch at all.
- Do not add a GitHub Actions workflow. The spec puts it out of scope.
- Do not mark anything unavailable. `unmatched` is a report, and deciding a product is delisted is a human call.
