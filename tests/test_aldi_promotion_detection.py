"""
Regression tests for Aldi promotion detection (MASA-94 / MASA-97).

Ensures:
1. A permanent grocery product does NOT pick up a `flash_sale` promotion
   from the site-wide "Specialbuys" navigation.
2. A genuine Special-Buy product page IS tagged as `flash_sale`.
3. Percentage discounts are stored in EUR, not as the raw percentage.
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


class AldiPromotionDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Avoid Selenium/DB setup — we only need the detection helpers.
        cls.scraper = SimpleLocalScraper.__new__(SimpleLocalScraper)

    def test_permanent_product_is_not_tagged_as_flash_sale(self) -> None:
        html = _load('aldi_permanent_product.html')
        result = self.scraper.detect_aldi_promotion_data(html, current_price=2.49)
        self.assertIsNone(
            result['promotion_type'],
            f"Permanent product should have no promotion, got {result}",
        )

    def test_special_buy_product_is_detected(self) -> None:
        html = _load('aldi_special_buy.html')
        result = self.scraper.detect_aldi_promotion_data(html, current_price=39.99)
        # Was/Now takes precedence over Special Buy — either is an acceptable
        # positive signal for a real Special Buy page, but promotion_type MUST
        # be set to something non-None.
        self.assertIsNotNone(result['promotion_type'])
        self.assertIn(
            result['promotion_type'],
            {'fixed_amount_off', 'flash_sale'},
        )
        # The Was price should be captured.
        self.assertEqual(result['original_price'], 59.99)

    def test_percentage_discount_is_stored_in_eur(self) -> None:
        html = """
        <html><body>
            <main>
                <div class="product-detail">
                    <h1>Some product</h1>
                    <span>Save 25% off</span>
                </div>
            </main>
        </body></html>
        """
        result = self.scraper.detect_aldi_promotion_data(html, current_price=10.00)
        self.assertEqual(result['promotion_type'], 'percentage_off')
        # 25% of €10.00 = €2.50, NOT the raw percentage 25.
        self.assertEqual(result['promotion_discount_value'], 2.50)


if __name__ == '__main__':
    unittest.main()
