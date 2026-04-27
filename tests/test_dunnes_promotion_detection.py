"""
Regression tests for Dunnes promotion detection.

Reproduces the false-positive multi-buy bug seen on 2026-04-23 where a
€1.99 pizza PDP was tagged with `Buy 2 for €11.50` from a "Customers
also bought" carousel. Mirrors the Aldi MASA-97 scope-fix test suite.
"""

from __future__ import annotations

import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(TESTS_DIR, 'fixtures')
PROJECT_ROOT = os.path.abspath(os.path.join(TESTS_DIR, '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from simple_local_to_prod import SimpleLocalScraper  # noqa: E402


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), 'r', encoding='utf-8') as fh:
        return fh.read()


class DunnesPromotionDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scraper = SimpleLocalScraper.__new__(SimpleLocalScraper)

    def test_carousel_multibuy_does_not_leak_into_product(self) -> None:
        """The €1.99 pizza must NOT pick up 'Buy 2 for €11.50' from a sibling carousel."""
        html = _load('dunnes_with_carousel.html')
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertIsNone(
            result['promotion_type'],
            f"Carousel multi-buy leaked into PDP detection: {result}",
        )

    def test_legitimate_multibuy_is_detected(self) -> None:
        """A real 'Any 3 for €5.00' promo on a €1.99 product must still be tagged."""
        html = _load('dunnes_real_multibuy.html')
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertEqual(result['promotion_type'], 'multi_buy')
        self.assertIn('3', result['promotion_text'])
        self.assertIn('5', result['promotion_text'])

    def test_sanity_check_accepts_realistic_deal(self) -> None:
        """Buy 2 for €3.99 on €1.99 → per-unit ≈ €2.00 (effectively no discount)
        but within tolerance, must be accepted."""
        self.assertTrue(
            self.scraper._is_plausible_multibuy(qty=2, total=3.99, current_price=1.99)
        )

    def test_sanity_check_rejects_inflated_deal(self) -> None:
        """The reported bug: Buy 2 for €11.50 on €1.99 → per-unit €5.75 = 2.89× price."""
        self.assertFalse(
            self.scraper._is_plausible_multibuy(qty=2, total=11.50, current_price=1.99)
        )

    def test_sanity_check_rejects_too_aggressive_deal(self) -> None:
        """Buy 2 for €0.50 on €1.99 → per-unit €0.25 = 12% of price (suspicious)."""
        self.assertFalse(
            self.scraper._is_plausible_multibuy(qty=2, total=0.50, current_price=1.99)
        )

    def test_sanity_check_passes_when_current_price_unknown(self) -> None:
        """If we don't have a current_price, accept and rely on DOM scoping."""
        self.assertTrue(
            self.scraper._is_plausible_multibuy(qty=3, total=5.00, current_price=None)
        )

    def test_scope_extractor_strips_carousel(self) -> None:
        """The scoped HTML must not contain text from the related-products section."""
        html = _load('dunnes_with_carousel.html')
        scoped = self.scraper._extract_dunnes_product_scope(html)
        self.assertIn('chicago town', scoped)
        self.assertNotIn('buy 2 for', scoped)
        self.assertNotIn('any 3 for', scoped)

    # ---------------------------------------------------------------------
    # MASA-115: gate fixed_amount_off / percentage_off on was/now evidence.
    # Audit (MASA-113) found 1,680 fixed_amount_off + 417 percentage_off
    # historical rows tagged on non-discounted Dunnes products. The badge
    # text alone is decorative copy; only treat it as a real promotion when
    # the page also shows an original_price > current_price.
    # ---------------------------------------------------------------------

    def test_decorative_save_badge_alone_does_not_tag_fixed_amount(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Decorative Test Product</h1>
            <span class="ProductPrice">€2.49</span>
            <div class="banner"><span>Save €0.50 across the store</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=2.49)
        self.assertIsNone(result['promotion_type'])
        self.assertIsNone(result['original_price'])

    def test_decorative_percentage_badge_alone_does_not_tag(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Decorative Test Product</h1>
            <span class="ProductPrice">€2.49</span>
            <div class="banner"><span>25% Off selected ranges</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=2.49)
        self.assertIsNone(result['promotion_type'])
        self.assertIsNone(result['original_price'])

    def test_decorative_half_price_alone_does_not_tag(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Decorative Test Product</h1>
            <span class="ProductPrice">€2.49</span>
            <div class="banner"><span>Half Price event this week</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=2.49)
        self.assertIsNone(result['promotion_type'])

    def test_fixed_amount_off_with_was_now_is_tagged(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Genuine Discount Product</h1>
            <span class="ProductPrice">€1.99</span>
            <span class="was">Was €2.49</span>
            <div class="banner"><span>Save €0.50</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertEqual(result['promotion_type'], 'fixed_amount_off')
        self.assertEqual(result['original_price'], 2.49)
        self.assertAlmostEqual(result['promotion_discount_value'], 0.50, places=2)

    def test_percentage_off_with_was_now_is_tagged(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Genuine Percentage Discount</h1>
            <span class="ProductPrice">€3.00</span>
            <span class="was">Was €4.00</span>
            <div class="banner"><span>25% Off</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=3.00)
        self.assertEqual(result['promotion_type'], 'percentage_off')
        self.assertEqual(result['original_price'], 4.00)
        self.assertAlmostEqual(result['promotion_discount_value'], 1.00, places=2)

    def test_strikethrough_was_price_unlocks_fixed_amount_tag(self) -> None:
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Strikethrough Discount</h1>
            <span class="ProductPrice">€1.99</span>
            <s>€2.79</s>
            <div class="banner"><span>Save €0.80</span></div>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertEqual(result['promotion_type'], 'fixed_amount_off')
        self.assertEqual(result['original_price'], 2.79)

    # ---------------------------------------------------------------------
    # MASA-115 follow-up (MASA-130 verifier): bundle math for multi_buy must
    # write back original_price + price_override so the row ships with
    # price < original_price. Pre-fix shipped 58/58 multi_buy rows in last
    # 24h with price == original_price (regex worked, bundle math didn't).
    # ---------------------------------------------------------------------

    def test_multibuy_writes_per_unit_price_and_original_price(self) -> None:
        """Any 3 for €5.00 on €1.99 shelf → per_unit ≈ €1.67, original = €1.99."""
        html = _load('dunnes_real_multibuy.html')
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertEqual(result['promotion_type'], 'multi_buy')
        self.assertEqual(result['original_price'], 1.99)
        self.assertEqual(result['price_override'], 1.67)
        self.assertAlmostEqual(result['promotion_discount_value'], 0.32, places=2)

    def test_multibuy_buy_x_for_y_writes_per_unit_override(self) -> None:
        """Buy 2 for €4.50 on €2.99 shelf → per_unit €2.25, original = €2.99.
        This is the exact pattern that produced 27/58 noisy prod rows."""
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Multi Buy Test</h1>
            <span class="ProductPrice">€2.99</span>
            <span class="promo-badge">Buy 2 for €4.50</span>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=2.99)
        self.assertEqual(result['promotion_type'], 'multi_buy')
        self.assertEqual(result['original_price'], 2.99)
        self.assertEqual(result['price_override'], 2.25)
        self.assertAlmostEqual(result['promotion_discount_value'], 0.74, places=2)

    def test_multibuy_no_override_when_per_unit_equals_shelf(self) -> None:
        """Buy 2 for €3.98 on €1.99 shelf → per_unit = €1.99 (no actual discount).
        Should still classify but NOT set price_override / original_price."""
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Trivial Multi Buy</h1>
            <span class="ProductPrice">€1.99</span>
            <span class="promo-badge">Buy 2 for €3.98</span>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertEqual(result['promotion_type'], 'multi_buy')
        self.assertIsNone(result.get('price_override'))
        # original_price stays None — caller falls back to shelf price.
        self.assertIsNone(result.get('original_price'))

    def test_loose_x_for_y_pattern_no_longer_matches(self) -> None:
        """Pre-fix, the bare `(\\d+) for (\\d+)` pattern matched random body
        copy and a "1 for 1" / "3 for 2" string would tag a multi_buy with
        no decimal price. MASA-115 dropped that pattern."""
        html = """
        <html><body><main>
          <div class="product-detail">
            <h1>Description Multi Buy</h1>
            <span class="ProductPrice">€1.99</span>
            <p>Pack contains 3 for 2 portions of cereal.</p>
          </div>
        </main></body></html>
        """
        result = self.scraper.detect_dunnes_promotion_data(html, current_price=1.99)
        self.assertIsNone(result['promotion_type'])


if __name__ == '__main__':
    unittest.main()
