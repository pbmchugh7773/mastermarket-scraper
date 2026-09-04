"""
Tests for repair_lidl_aliases — the Lidl broken-alias repair tool.

Pure-Python, no network. See
docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import repair_lidl_aliases as rla  # noqa: E402
from repair_lidl_aliases import (  # noqa: E402
    apply_repairs,
    build_match_product,
    build_repair_record,
    build_unmatched_record,
    check_url_liveness,
    choose_token_outcome,
    classify_liveness,
    decide_slug_size,
    find_slug_candidate,
    index_sitemap_by_slug,
    parse_lidl_url,
    REPAIR_THRESHOLD,
    repair_one,
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


PDP = "https://www.lidl.ie/p/parmigiano-reggiano-dop/p111"
PASS1_URL = "https://www.lidl.ie/p/parmigiano-reggiano-dop/p222"
PASS2_URL = "https://www.lidl.ie/p/italiamo-parmigiano-reggiano-dop/p333"
CATEGORY_URL = "https://www.lidl.ie/c/cheese-and-dairy/s10068094"


def _size_html(size):
    """Minimal page HTML that extract_size_from_html() reads a size out of."""
    return f"<html><body><span>{size}</span></body></html>"


NO_SIZE_HTML = "<html><body><span>Delicious and creamy</span></body></html>"


class LivenessRedirectTests(unittest.TestCase):
    """A 200 that landed off the Lidl PDP pattern must not read as recovered."""

    def test_200_that_did_not_redirect_is_recovered(self):
        self.assertEqual(classify_liveness(200, PDP, PDP), "recovered")

    def test_200_redirected_to_another_pdp_is_recovered(self):
        """www→non-www and locale canonicalisation are legitimate."""
        self.assertEqual(classify_liveness(200, PDP, PASS1_URL), "recovered")

    def test_200_redirected_to_a_category_page_is_inconclusive(self):
        """The failure mode this guards: a dead PDP bounced to a listing page
        would otherwise mark the whole fleet self-healed and propose nothing."""
        self.assertEqual(classify_liveness(200, PDP, CATEGORY_URL), "inconclusive")

    def test_404_stays_broken_regardless_of_where_it_landed(self):
        self.assertEqual(classify_liveness(404, PDP, CATEGORY_URL), "broken")

    def test_missing_final_url_leaves_a_200_recovered(self):
        """No redirect evidence either way — do not invent a rejection."""
        self.assertEqual(classify_liveness(200, PDP, None), "recovered")
        self.assertEqual(classify_liveness(200), "recovered")


class CheckUrlLivenessTests(unittest.TestCase):
    def test_returns_status_and_final_url(self):
        resp = mock.Mock(status_code=200, url=CATEGORY_URL)
        with mock.patch.object(rla.requests, "get", return_value=resp):
            self.assertEqual(check_url_liveness(PDP), (200, CATEGORY_URL))

    def test_request_failure_is_none_none(self):
        with mock.patch.object(
            rla.requests, "get", side_effect=rla.requests.RequestException("boom")
        ):
            self.assertEqual(check_url_liveness(PDP), (None, None))


class RepairOneTests(unittest.TestCase):
    """
    Routing between pass 1 and pass 2. Every HTML fetch is stubbed, so these
    are offline and deterministic.

    Fixture: the alias points at .../parmigiano-reggiano-dop/p111. The sitemap
    carries that same slug under a new SKU (p222 — pass 1's candidate) and a
    brand-prefixed variant (p333) that pass 2 scores at 1.0. Which URL comes
    back therefore says which pass produced the repair.
    """

    def setUp(self):
        self.alias = _alias(770, scraper_url=PDP)
        self.product = build_match_product(
            {
                "id": 55,
                "name": "Parmigiano Reggiano DOP 500g",
                "brand": "Italiamo",
                "unit": "g",
            }
        )
        self.sitemap = [
            _entry("parmigiano-reggiano-dop", "222"),
            _entry("italiamo-parmigiano-reggiano-dop", "333"),
        ]
        self.by_slug = index_sitemap_by_slug(self.sitemap)
        self.fetched = []

    def _run(self, pages):
        def fake_fetch(url, fetch_log):
            self.fetched.append(url)
            return pages.get(url)

        with mock.patch.object(rla, "fetch_lidl_page", side_effect=fake_fetch):
            return repair_one(
                self.alias, self.product, self.sitemap, self.by_slug, {}
            )

    def test_pass1_size_match_is_accepted(self):
        repair, miss = self._run({PASS1_URL: _size_html("500g")})
        self.assertIsNone(miss)
        self.assertEqual(repair["method"], "slug_exact")
        self.assertEqual(repair["new_url"], PASS1_URL)
        self.assertEqual(self.fetched, [PASS1_URL], "pass 1 should stop on a match")

    def test_pass1_page_fetched_but_sizeless_is_accepted_as_unverified(self):
        """'unverified' keeps its meaning: the page WAS retrieved, it just had
        no size to check the slug against."""
        repair, miss = self._run({PASS1_URL: NO_SIZE_HTML})
        self.assertIsNone(miss)
        self.assertEqual(repair["method"], "slug_exact_unverified")
        self.assertEqual(repair["new_url"], PASS1_URL)
        self.assertIsNone(repair["html_size"])

    def test_pass1_size_mismatch_falls_through_to_pass2(self):
        repair, miss = self._run(
            {PASS1_URL: _size_html("750g"), PASS2_URL: _size_html("500g")}
        )
        self.assertIsNone(miss)
        self.assertEqual(repair["method"], "token_match")
        self.assertEqual(repair["new_url"], PASS2_URL)

    def test_pass1_fetch_failure_falls_through_to_pass2(self):
        """
        Regression: fetch_lidl_page returns None both for a transport failure
        and for a candidate that itself 404s. Pass 1 used to read that as
        decide_slug_size(size, None) == 'unverified' and accept it, proposing a
        URL that was never retrieved — under --apply, one dead URL written over
        another. Pass 2 always rejected the same condition, so the weaker
        evidence path was the more permissive one.
        """
        repair, miss = self._run({PASS1_URL: None, PASS2_URL: _size_html("500g")})
        self.assertIsNone(miss)
        self.assertEqual(repair["method"], "token_match")
        self.assertEqual(repair["new_url"], PASS2_URL)
        self.assertNotEqual(
            repair["new_url"], PASS1_URL, "never propose a URL that 404'd on us"
        )

    def test_pass1_fetch_failure_with_no_pass2_rescue_is_unmatched(self):
        repair, miss = self._run({PASS1_URL: None, PASS2_URL: None})
        self.assertIsNone(repair)
        self.assertEqual(miss["reason"], "html_fetch_failed")
        self.assertNotIn("new_url", miss)

    def test_pass2_size_mismatch_everywhere_is_unmatched(self):
        repair, miss = self._run(
            {PASS1_URL: _size_html("750g"), PASS2_URL: _size_html("750g")}
        )
        self.assertIsNone(repair)
        self.assertEqual(miss["reason"], "size_mismatch")

    def test_unknown_mm_size_short_circuits_pass2_without_fetching(self):
        """Pass 2 can only accept on a size equality, so a product with no
        derivable size is a foregone rejection — do not pay a live fetch per
        candidate to discover that, and do not let the reason be a Counter
        tie-break against no_html_size."""
        self.product = build_match_product(
            {"id": 56, "name": "Parmigiano Reggiano DOP", "brand": "Italiamo",
             "unit": "portion"}
        )
        self.assertIsNone(self.product["size"])
        self.alias = _alias(771, scraper_url="https://www.lidl.ie/p/gone-slug/p111")

        repair, miss = self._run({PASS2_URL: _size_html("500g")})
        self.assertIsNone(repair)
        self.assertEqual(miss["reason"], "unknown_mm_size")
        self.assertEqual(self.fetched, [], "no candidate should have been fetched")


def _product_row(pid, name, brand, unit):
    return {"id": pid, "name": name, "brand": brand, "unit": unit}


class MainTests(unittest.TestCase):
    """
    End-to-end orchestration with every network call stubbed: the sitemap, the
    API login, the alias and product fetches, the liveness probe, the page
    fetch and the PUT.
    """

    def setUp(self):
        self.sitemap = [
            _entry("parmigiano-reggiano-dop", "222"),
            _entry("italiamo-parmigiano-reggiano-dop", "333"),
        ]
        self.repairable = _alias(770, product_id=55, scraper_url=PDP)
        self.unmatchable = _alias(
            771, product_id=56, scraper_url="https://www.lidl.ie/p/gone-slug/p999"
        )
        self.products = {
            55: _product_row(55, "Parmigiano Reggiano DOP 500g", "Italiamo", "g"),
            56: _product_row(56, "Mystery Item", "Kania", "portion"),
        }
        self.pages = {PASS1_URL: _size_html("500g"), PASS2_URL: _size_html("500g")}

    def _run_main(self, argv=(), aliases=None, liveness=None, put_side_effect=None):
        """(exit_code, proposal_dict, put_calls) for a fully stubbed run."""
        aliases = self.repairable if aliases is None else aliases
        aliases = aliases if isinstance(aliases, list) else [aliases]
        liveness = liveness or {}
        put_calls = []

        def fake_liveness(url):
            return liveness.get(url, (404, url))

        def fake_page(url, fetch_log):
            return self.pages.get(url)

        def fake_put(alias_id, payload, token):
            put_calls.append((alias_id, payload, token))
            if put_side_effect is not None:
                put_side_effect(alias_id, payload, token, self.out_path)

        with tempfile.TemporaryDirectory() as tmp:
            def fake_path(p):
                # Keep main's filename, redirect it out of /tmp for the test.
                self.out_path = pathlib.Path(tmp) / pathlib.Path(p).name
                return self.out_path

            with mock.patch.object(
                rla, "fetch_lidl_sitemap_urls", return_value=self.sitemap
            ), mock.patch.object(
                rla, "_api_login", return_value="tok"
            ), mock.patch.object(
                rla, "fetch_lidl_aliases", return_value=aliases
            ), mock.patch.object(
                rla, "fetch_all_products_by_id", return_value=self.products
            ), mock.patch.object(
                rla, "check_url_liveness", side_effect=fake_liveness
            ), mock.patch.object(
                rla, "fetch_lidl_page", side_effect=fake_page
            ), mock.patch.object(
                rla, "_put_scraper_url", side_effect=fake_put
            ), mock.patch.object(
                rla, "Path", fake_path
            ), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = rla.main(list(argv))
            proposal = json.loads(self.out_path.read_text())
        return code, proposal, put_calls

    def test_run_without_apply_issues_no_writes(self):
        """The core safety property: proposal-only unless --apply is passed."""
        code, proposal, puts = self._run_main(argv=[])
        self.assertEqual(code, 0)
        self.assertEqual(len(proposal["repairs"]), 1, "a repair was available")
        self.assertEqual(puts, [], "no PUT may be issued without --apply")
        self.assertEqual(proposal["applied"], 0)
        self.assertEqual(proposal["apply_failures"], 0)

    def test_apply_writes_the_proposed_url(self):
        code, proposal, puts = self._run_main(argv=["--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(len(puts), 1)
        alias_id, payload, token = puts[0]
        self.assertEqual(alias_id, 770)
        self.assertEqual(payload, {"scraper_url": proposal["repairs"][0]["new_url"]})
        self.assertEqual(token, "tok")
        self.assertEqual(proposal["applied"], 1)

    def test_unmatched_aliases_are_never_written(self):
        code, proposal, puts = self._run_main(
            argv=["--apply"], aliases=[self.repairable, self.unmatchable]
        )
        self.assertEqual([p["alias_id"] for p in proposal["repairs"]], [770])
        self.assertEqual([u["alias_id"] for u in proposal["unmatched"]], [771])
        self.assertEqual(
            [alias_id for alias_id, _, _ in puts],
            [770],
            "an unmatched alias must never reach the write path",
        )

    def test_proposal_is_on_disk_before_the_first_put(self):
        """
        Every old_url lives only in memory until the JSON lands. If the PUTs
        ran first, a crash mid-batch would leave rewritten aliases with nothing
        to roll back from.
        """
        seen = []

        def inspect(alias_id, payload, token, out_path):
            seen.append(
                json.loads(out_path.read_text()) if out_path.exists() else None
            )

        code, proposal, puts = self._run_main(
            argv=["--apply"], put_side_effect=inspect
        )
        self.assertEqual(len(seen), 1)
        on_disk = seen[0]
        self.assertIsNotNone(on_disk, "proposal file must exist before any PUT")
        self.assertEqual(
            [r["old_url"] for r in on_disk["repairs"]],
            [r["old_url"] for r in proposal["repairs"]],
            "the full repair list must be persisted before the first write",
        )

    def test_apply_failure_is_counted_in_the_rewritten_file_and_exit_code(self):
        def boom(alias_id, payload, token, out_path):
            raise RuntimeError("api down")

        code, proposal, puts = self._run_main(argv=["--apply"], put_side_effect=boom)
        self.assertEqual(code, 1)
        self.assertEqual(proposal["applied"], 0)
        self.assertEqual(proposal["apply_failures"], 1)

    def test_recovered_alias_is_reported_not_repaired(self):
        code, proposal, puts = self._run_main(
            argv=["--apply"], liveness={PDP: (200, PDP)}
        )
        self.assertEqual(proposal["repairs"], [])
        self.assertEqual(proposal["unmatched"][0]["reason"], "alias_recovered")
        self.assertEqual(proposal["verified_404_count"], 0)
        self.assertEqual(puts, [], "a healthy alias must not be rewritten")

    def test_inconclusive_alias_is_reported_not_repaired(self):
        code, proposal, puts = self._run_main(
            argv=["--apply"], liveness={PDP: (403, PDP)}
        )
        self.assertEqual(proposal["repairs"], [])
        self.assertEqual(
            proposal["unmatched"][0]["reason"], "liveness_check_inconclusive"
        )
        self.assertEqual(puts, [])

    def test_200_redirected_off_the_pdp_is_not_treated_as_recovered(self):
        """A category-page bounce is inconclusive, not proof of recovery — and
        an inconclusive alias is still never written."""
        code, proposal, puts = self._run_main(
            argv=["--apply"], liveness={PDP: (200, CATEGORY_URL)}
        )
        self.assertEqual(
            proposal["unmatched"][0]["reason"], "liveness_check_inconclusive"
        )
        self.assertEqual(puts, [])

    def test_limit_caps_the_number_of_aliases_processed(self):
        code, proposal, puts = self._run_main(
            argv=["--limit", "1"], aliases=[self.repairable, self.unmatchable]
        )
        self.assertEqual(
            len(proposal["repairs"]) + len(proposal["unmatched"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
