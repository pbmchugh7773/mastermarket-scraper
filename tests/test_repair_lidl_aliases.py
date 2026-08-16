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
    classify_liveness,
    decide_slug_size,
    find_slug_candidate,
    index_sitemap_by_slug,
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


def _entry(slug, sku):
    """A sitemap entry shaped like fetch_lidl_sitemap_urls() returns it."""
    return {
        "url": f"https://www.lidl.ie/p/{slug}/p{sku}",
        "slug": slug.replace("-", " "),
        "sku": sku,
        "norm": slug.replace("-", " "),
        "slug_size": None,
    }


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


if __name__ == "__main__":
    unittest.main()
