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
    apply_repairs,
    build_match_product,
    build_repair_record,
    build_unmatched_record,
    choose_token_outcome,
    classify_liveness,
    decide_slug_size,
    find_slug_candidate,
    index_sitemap_by_slug,
    parse_lidl_url,
    REPAIR_THRESHOLD,
    select_broken_aliases,
    select_token_candidates,
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


if __name__ == "__main__":
    unittest.main()
