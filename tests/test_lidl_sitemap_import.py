"""
Regression tests for MASA-155: Lidl sitemap-import pipeline (Phase 1.2 of MASA-137).

Codifies the ad-hoc smoke tests from heartbeats #8/#9/#10 into a durable
unittest suite. Covers all 3 gates of the pipeline:

  url_gate        bad_url_pattern, already_imported, accept
  payload_gate    noindex_meta, not_product_jsonld, missing_image, no_price,
                  expired_offer, accept (+ field extraction correctness)
  insert_gate     unknown_breadcrumb_leaf, unknown_brand (empty + random),
                  competing_brand_in_slug, image_host_not_whitelisted, accept
                  (+ confidence scoring at §4.2 weights / §4.3 thresholds)

These tests are pure-Python and do NOT touch the DB or the network. The
synthetic HTML fixtures are inline so the file is self-contained — this
matches the style of test_aldi_promotion_detection.py and
test_dunnes_promotion_detection.py.
"""
from __future__ import annotations

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lidl_sitemap_import import (  # noqa: E402
    CONFIDENCE_AUTO_ACCEPT,
    CONFIDENCE_HUMAN_REVIEW,
    LIDL_BREADCRUMB_TO_HUB,
    LIDL_HUB_SET,
    LIDL_IMAGE_HOST_WHITELIST,
    _Candidate,
    insert_gate,
    payload_gate,
    url_gate,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _good_candidate(**overrides) -> _Candidate:
    """Build a baseline _Candidate that should pass insert_gate."""
    base = dict(
        url="https://www.lidl.ie/p/vemondo-almond-drink-1l/p99",
        sku="99",
        name="Vemondo Almond Drink 1L",
        brand="Vemondo",
        image_url="https://imgproxy-retcat.assets.schwarz/abc.jpg",
        price=1.59,
        breadcrumb_leaf="dairy",
        size_text="1l",
    )
    base.update(overrides)
    fields = _Candidate.__dataclass_fields__
    return _Candidate(**{k: v for k, v in base.items() if k in fields})


def _html(*, jsonld: str = "", title: str = "", h1: str = "", noindex: bool = False) -> str:
    parts = ["<html>", "<head>"]
    if noindex:
        parts.append('<meta name="robots" content="noindex,nofollow">')
    if title:
        parts.append(f"<title>{title}</title>")
    parts.append("</head><body>")
    if h1:
        parts.append(f"<h1>{h1}</h1>")
    if jsonld:
        parts.append(f'<script type="application/ld+json">{jsonld}</script>')
    parts.append("</body></html>")
    return "".join(parts)


_VEMONDO_JSONLD = (
    '{"@type":"Product","name":"Vemondo Almond Drink 1L",'
    '"brand":{"@type":"Brand","name":"Vemondo"},'
    '"image":["https://imgproxy-retcat.assets.schwarz/abc.jpg"],'
    '"offers":{"price":"1.59","priceCurrency":"EUR"},'
    '"category":"Drinks > Almond"}'
)


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------

class TestModuleInvariants(unittest.TestCase):
    """Smoke checks that constants match MASA-138 spec."""

    def test_hub_set_has_8_entries(self):
        self.assertEqual(len(LIDL_HUB_SET), 8)

    def test_breadcrumb_map_only_targets_known_hubs(self):
        # Every value in the breadcrumb→hub map must be in LIDL_HUB_SET.
        unknown = {
            hub for hub in LIDL_BREADCRUMB_TO_HUB.values()
            if hub not in LIDL_HUB_SET
        }
        self.assertFalse(
            unknown,
            f"breadcrumb map points at hubs outside the canonical set: {unknown}",
        )

    def test_thresholds_are_ordered(self):
        self.assertGreater(CONFIDENCE_AUTO_ACCEPT, CONFIDENCE_HUMAN_REVIEW)
        self.assertGreaterEqual(CONFIDENCE_HUMAN_REVIEW, 0.0)

    def test_image_whitelist_includes_lidl_cdn(self):
        # Schwarz Group imgproxy CDN — verified empirically against live pages.
        self.assertIn("imgproxy-retcat.assets.schwarz", LIDL_IMAGE_HOST_WHITELIST)


# ---------------------------------------------------------------------------
# Gate 1 — URL gate
# ---------------------------------------------------------------------------

class TestUrlGate(unittest.TestCase):
    def test_accepts_well_formed_pdp_url(self):
        cand, rej = url_gate(
            "https://www.lidl.ie/p/some-product-slug/p12345", set()
        )
        self.assertIsNotNone(cand)
        self.assertIsNone(rej)
        self.assertEqual(cand.sku, "12345")

    def test_rejects_non_pdp_url(self):
        _, rej = url_gate("https://www.lidl.ie/some/random/path", set())
        self.assertIsNotNone(rej)
        self.assertEqual(rej.reason_code, "bad_url_pattern")

    def test_rejects_already_imported_sku(self):
        _, rej = url_gate(
            "https://www.lidl.ie/p/slug/p12345", {"12345"}
        )
        self.assertIsNotNone(rej)
        self.assertEqual(rej.reason_code, "already_imported")


# ---------------------------------------------------------------------------
# Gate 2 — Payload gate
# ---------------------------------------------------------------------------

class TestPayloadGate(unittest.TestCase):
    def _candidate_with_html(self, html: str) -> _Candidate:
        cand = _Candidate(url="https://www.lidl.ie/p/x/p1", sku="1")
        cand.raw_html = html
        return cand

    def test_rejects_noindex_meta(self):
        # noindex check fires BEFORE JSON-LD parse — even if Product is
        # well-formed, the page is rejected.
        html = _html(jsonld=_VEMONDO_JSONLD, noindex=True)
        _, rej = payload_gate(self._candidate_with_html(html))
        self.assertEqual(rej.reason_code, "noindex_meta")

    def test_rejects_pages_without_product_jsonld(self):
        _, rej = payload_gate(self._candidate_with_html("<html><body>hi</body></html>"))
        self.assertEqual(rej.reason_code, "not_product_jsonld")

    def test_rejects_product_without_image(self):
        jsonld = '{"@type":"Product","name":"X","offers":{"price":"1.99"}}'
        _, rej = payload_gate(self._candidate_with_html(_html(jsonld=jsonld)))
        self.assertEqual(rej.reason_code, "missing_image")

    def test_rejects_product_without_price(self):
        jsonld = '{"@type":"Product","name":"X","image":"http://x/y.jpg"}'
        _, rej = payload_gate(self._candidate_with_html(_html(jsonld=jsonld)))
        self.assertEqual(rej.reason_code, "no_price")

    def test_rejects_expired_offer_phrase_in_visible_text(self):
        html = _html(jsonld=_VEMONDO_JSONLD, h1="Out of stock")
        _, rej = payload_gate(self._candidate_with_html(html))
        self.assertEqual(rej.reason_code, "expired_offer")

    def test_accepts_well_formed_product_with_field_extraction(self):
        cand = self._candidate_with_html(
            _html(jsonld=_VEMONDO_JSONLD, title="Vemondo Almond Drink 1L")
        )
        out, rej = payload_gate(cand)
        self.assertIsNone(rej)
        self.assertEqual(out.name, "Vemondo Almond Drink 1L")
        self.assertEqual(out.brand, "Vemondo")
        self.assertEqual(
            out.image_url, "https://imgproxy-retcat.assets.schwarz/abc.jpg"
        )
        self.assertEqual(out.price, 1.59)
        # Path-style category "Drinks > Almond" → leaf "almond" lower-case.
        self.assertEqual(out.breadcrumb_leaf, "almond")

    def test_extracts_price_with_comma_decimal_separator(self):
        # Some EU JSON-LD emits "1,59" instead of "1.59".
        jsonld = (
            '{"@type":"Product","name":"X","brand":"Y",'
            '"image":"http://imgproxy-retcat.assets.schwarz/x.jpg",'
            '"offers":{"price":"1,59","priceCurrency":"EUR"},"category":"Bakery"}'
        )
        out, rej = payload_gate(self._candidate_with_html(_html(jsonld=jsonld)))
        self.assertIsNone(rej)
        self.assertEqual(out.price, 1.59)

    def test_extracts_price_from_pricespecification_fallback(self):
        jsonld = (
            '{"@type":"Product","name":"X","brand":"Y",'
            '"image":"http://imgproxy-retcat.assets.schwarz/x.jpg",'
            '"offers":{"priceSpecification":{"price":"2.49"}},'
            '"category":"Bakery"}'
        )
        out, rej = payload_gate(self._candidate_with_html(_html(jsonld=jsonld)))
        self.assertIsNone(rej)
        self.assertEqual(out.price, 2.49)


# ---------------------------------------------------------------------------
# Gate 3 — Insert gate
# ---------------------------------------------------------------------------

class TestInsertGate(unittest.TestCase):
    def test_rejects_unknown_breadcrumb_leaf(self):
        _, rej = insert_gate(_good_candidate(breadcrumb_leaf="lidl surprises"))
        self.assertEqual(rej.reason_code, "unknown_breadcrumb_leaf")

    def test_rejects_empty_brand(self):
        _, rej = insert_gate(_good_candidate(brand=""))
        self.assertEqual(rej.reason_code, "unknown_brand")

    def test_rejects_random_unknown_brand(self):
        _, rej = insert_gate(_good_candidate(brand="RandomMadeUpBrandX"))
        self.assertEqual(rej.reason_code, "unknown_brand")

    def test_rejects_competing_brand_in_slug(self):
        # Vemondo product but slug names Alpro — the MASA-135 v3 reject.
        _, rej = insert_gate(_good_candidate(
            url="https://www.lidl.ie/p/alpro-almond-drink-1l/p99",
            brand="Vemondo",
        ))
        self.assertEqual(rej.reason_code, "competing_brand_in_slug")

    def test_rejects_image_host_not_whitelisted(self):
        _, rej = insert_gate(_good_candidate(
            image_url="https://random-cdn.example.com/img.jpg"
        ))
        self.assertEqual(rej.reason_code, "image_host_not_whitelisted")

    def test_accepts_lidl_own_brand_at_auto_accept_threshold(self):
        # Vemondo + dairy + Schwarz CDN — own-brand boundary case scoring 0.85.
        proposal, rej = insert_gate(_good_candidate())
        self.assertIsNone(rej)
        self.assertEqual(proposal.proposed_category, "Fresh Food")
        self.assertGreaterEqual(proposal.confidence, CONFIDENCE_AUTO_ACCEPT)

    def test_accepts_national_brand_at_human_review_band(self):
        # Coca-Cola in soft drinks — national brand, no own-brand bonus.
        proposal, rej = insert_gate(_good_candidate(
            url="https://www.lidl.ie/p/coca-cola-zero-2l/p77",
            brand="Coca-Cola",
            breadcrumb_leaf="soft drinks",
        ))
        self.assertIsNone(rej)
        self.assertEqual(proposal.proposed_category, "Drinks")
        self.assertGreaterEqual(proposal.confidence, CONFIDENCE_HUMAN_REVIEW)
        self.assertLess(proposal.confidence, CONFIDENCE_AUTO_ACCEPT)


if __name__ == "__main__":
    unittest.main()
