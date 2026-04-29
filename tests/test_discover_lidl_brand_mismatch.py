"""
Regression tests for MASA-135: Lidl discovery v3 brand-mismatch hard reject
and pool-aware candidate query.

Covers the exact near-miss called out in the issue acceptance criterion:
  - MM product 7303 brand=Vemondo, name=Barista Oat milk, size=1L
  - Lidl URL slug "alpro-alpro-barista-oat-milk"
  - Must be hard-rejected with reason `competing_brand_in_slug` BEFORE any
    HTML fetch.

These tests are pure-Python and do NOT touch the DB or the network.
"""
from __future__ import annotations

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from discover_lidl_aliases import (  # noqa: E402
    POOL_ALDI,
    POOL_LIDL_OWN,
    POOL_CHOICES,
    LIDL_OWN_BRANDS,
    COMPETING_BRANDS,
    _brand_in_norm,
    _brand_mismatch_reason,
    _slug_brand_token,
    apply_brand_mismatch_filter,
    normalise,
    _proposal_record,
)


def _candidate(pid: int, name: str, brand: str, size: str | None = "1l", score: float = 0.62):
    """Build a Phase-1 candidate dict matching what main() puts in by_url."""
    return {
        "id": pid,
        "name": name,
        "brand": brand,
        "unit": "",
        "norm": normalise(f"{brand} {name}"),
        "size": size,
        "variant": set(),
        "score": score,
    }


class TestBrandHelpers(unittest.TestCase):
    def test_brand_token_membership_handles_multi_word(self):
        self.assertTrue(_brand_in_norm("coca cola", "coca cola classic 330ml"))
        self.assertFalse(_brand_in_norm("coca cola", "cocacola classic"))
        self.assertTrue(_brand_in_norm("alpro", "alpro alpro barista oat milk"))
        self.assertFalse(_brand_in_norm("alpro", "alpha alphabet"))

    def test_slug_brand_token_finds_known_brand(self):
        self.assertEqual(_slug_brand_token("alpro alpro barista oat milk"), "alpro")
        self.assertEqual(_slug_brand_token("milbona greek style yoghurt"), "milbona")
        self.assertIsNone(_slug_brand_token("oat milk barista 1l"))

    def test_slug_brand_token_prefers_longer_match(self):
        # If "trattoria verdi" is in KNOWN_BRAND_TOKENS, it should win over the
        # bare word "verdi" if that were ever added. The function iterates
        # longest-first so this is structural — assert by sample.
        self.assertEqual(
            _slug_brand_token("trattoria verdi pasta sauce"),
            "trattoria verdi",
        )


class TestBrandMismatchReason(unittest.TestCase):
    def test_vemondo_alpro_is_hard_reject(self):
        # Exact regression case from the MASA-135 issue body.
        slug_norm = normalise("alpro alpro barista oat milk")
        reason = _brand_mismatch_reason("Vemondo", slug_norm)
        self.assertEqual(reason, "competing_brand_in_slug")

    def test_milbona_milbona_is_kept(self):
        # MM brand and slug brand agree → no reject signal.
        slug_norm = normalise("milbona greek style yoghurt 500g")
        self.assertIsNone(_brand_mismatch_reason("Milbona", slug_norm))

    def test_unknown_slug_brand_lets_phase2_decide(self):
        # If the slug doesn't advertise a known brand, the brand-mismatch
        # filter must NOT veto — Phase 2 size/variant decides.
        slug_norm = normalise("oat milk barista style 1l")
        self.assertIsNone(_brand_mismatch_reason("Vemondo", slug_norm))

    def test_brand_with_lidl_prefix_alias_still_matches(self):
        # DB has rows like "Lidl, Milbona" / "Bio Organic, Lidl, Milbona".
        # After normalise() the milbona token is still present, so it must
        # match a Milbona slug (otherwise we'd reject 145 own-brand rows).
        slug_norm = normalise("milbona protein yoghurt 200g")
        self.assertIsNone(_brand_mismatch_reason("Lidl, Milbona", slug_norm))
        self.assertIsNone(_brand_mismatch_reason("Bio Organic, Lidl, Milbona", slug_norm))

    def test_national_vs_different_national_brand(self):
        # MM brand "Heinz" against an Hellmann slug must be rejected
        # regardless of size — the Hellmann's→Heinz collision is exactly the
        # symmetric case the issue calls out.
        slug_norm = normalise("hellmann real mayonnaise 750ml")
        self.assertEqual(
            _brand_mismatch_reason("Heinz", slug_norm),
            "competing_brand_in_slug",
        )

    def test_empty_mm_brand_is_rejected_when_slug_carries_brand(self):
        # Defensive: missing MM brand against a brand-bearing slug → reject.
        slug_norm = normalise("alpro barista oat milk")
        self.assertEqual(
            _brand_mismatch_reason("", slug_norm),
            "competing_brand_in_slug",
        )


class TestApplyBrandMismatchFilter(unittest.TestCase):
    def _vemondo_url(self):
        return "https://www.lidl.ie/p/alpro-alpro-barista-oat-milk/p10056670"

    def _milbona_url(self):
        return "https://www.lidl.ie/p/milbona-greek-style-yoghurt/p10012345"

    def test_vemondo_alpro_rejected_in_phase15(self):
        url = self._vemondo_url()
        by_url = {
            url: [_candidate(7303, "Barista Oat milk", "Vemondo")],
        }
        filtered, rejections = apply_brand_mismatch_filter(by_url)

        # Acceptance criterion: this proposal is rejected with the named
        # reason, and the filter prunes it from the by_url before HTML fetch.
        self.assertNotIn(url, filtered)
        self.assertEqual(len(rejections), 1)
        rec = rejections[0]
        self.assertEqual(rec["reason"], "competing_brand_in_slug")
        self.assertEqual(rec["product_id"], 7303)
        self.assertEqual(rec["slug_brand_token"], "alpro")

    def test_milbona_milbona_kept(self):
        url = self._milbona_url()
        by_url = {
            url: [_candidate(101, "Greek Style Yoghurt", "Milbona", size="500g")],
        }
        filtered, rejections = apply_brand_mismatch_filter(by_url)
        self.assertIn(url, filtered)
        self.assertEqual(len(filtered[url]), 1)
        self.assertEqual(rejections, [])

    def test_mixed_group_partial_filter(self):
        # Same Lidl URL, two MM candidates: one Milbona (kept), one bogus
        # Vemondo (rejected). The URL should remain in by_url with only the
        # surviving candidate.
        url = self._milbona_url()
        by_url = {
            url: [
                _candidate(101, "Greek Style Yoghurt", "Milbona", size="500g"),
                _candidate(202, "Barista Oat milk", "Vemondo", size="1l"),
            ],
        }
        filtered, rejections = apply_brand_mismatch_filter(by_url)
        # Milbona slug, Vemondo brand → no known competing brand on slug for
        # Vemondo specifically? slug = "milbona greek style yoghurt" → milbona
        # is the slug's brand, Vemondo's brand differs → reject Vemondo.
        self.assertEqual(len(filtered.get(url, [])), 1)
        self.assertEqual(filtered[url][0]["id"], 101)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["product_id"], 202)
        self.assertEqual(rejections[0]["reason"], "competing_brand_in_slug")


class TestPoolConstants(unittest.TestCase):
    def test_default_pool_is_aldi_cross_list(self):
        # v2 behaviour preserved unless caller opts in.
        self.assertEqual(POOL_ALDI, "aldi-cross-list")
        self.assertEqual(POOL_LIDL_OWN, "lidl-own-brand")
        self.assertEqual(POOL_CHOICES, (POOL_ALDI, POOL_LIDL_OWN))

    def test_lidl_own_brands_cover_issue_table(self):
        # The set named in the MASA-135 findings table must be present.
        for required in ("milbona", "italiamo", "vemondo", "combino",
                         "solevita", "newgate", "lupilu", "dulano", "pilos"):
            self.assertIn(required, LIDL_OWN_BRANDS, msg=f"missing {required!r}")

    def test_competing_brands_include_alpro(self):
        # Catches the exact Vemondo→Alpro near-miss.
        self.assertIn("alpro", COMPETING_BRANDS)


if __name__ == "__main__":
    unittest.main()
