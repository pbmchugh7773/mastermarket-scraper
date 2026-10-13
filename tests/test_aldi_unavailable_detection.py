"""
Regression tests for Aldi "currently not available" detection (2026-10-13).

Aldi delists products in two ways: HTTP 200 with no price and a rendered
"This product is currently not available." banner, or HTTP 410 with a
"Page Error" page (status invisible to Selenium). Without detection those
aliases fail as "Failed to extract price" (~42s each through the implicit
waits) on every batch and alias_lifecycle never retires them, because a parse
failure is not a dead-URL reason.

Ensures:
1. The rendered banner + empty JSON-LD price, and the "Page Error" page,
   are detected, each with its own reason.
2. A priced page is NOT unavailable even though the same string sits in the
   __NUXT_DATA__ i18n payload of every Aldi page.
3. Both resulting error messages are ones alias_lifecycle treats as dead-URL.
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

from alias_lifecycle import classify_dead_reason  # noqa: E402
from simple_local_to_prod import (  # noqa: E402
    ALDI_GONE_REASON,
    ALDI_UNAVAILABLE_REASON,
    SimpleLocalScraper,
)


def _load(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), 'r', encoding='utf-8') as fh:
        return fh.read()


class AldiUnavailableDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Avoid Selenium/DB setup — we only need the detection helper.
        cls.scraper = SimpleLocalScraper.__new__(SimpleLocalScraper)

    def test_unavailable_page_is_detected(self) -> None:
        html = _load('aldi_unavailable_product.html')
        self.assertEqual(self.scraper.aldi_unavailable_reason(html), ALDI_UNAVAILABLE_REASON)

    def test_gone_page_is_detected(self) -> None:
        html = _load('aldi_gone_product.html')
        self.assertEqual(self.scraper.aldi_unavailable_reason(html), ALDI_GONE_REASON)

    def test_priced_page_with_i18n_string_is_not_unavailable(self) -> None:
        html = _load('aldi_available_product_with_i18n.html')
        self.assertIsNone(self.scraper.aldi_unavailable_reason(html))

    def test_existing_priced_fixtures_are_not_unavailable(self) -> None:
        for name in ('aldi_permanent_product.html', 'aldi_special_buy.html'):
            with self.subTest(fixture=name):
                self.assertIsNone(self.scraper.aldi_unavailable_reason(_load(name)))

    def test_banner_with_real_price_is_not_unavailable(self) -> None:
        # Defensive: if Aldi ever shows the banner next to a real price
        # (e.g. online-only out of stock), keep scraping the price.
        html = _load('aldi_unavailable_product.html').replace('"price":""', '"price":"1.99"')
        self.assertIsNone(self.scraper.aldi_unavailable_reason(html))

    def test_reasons_are_dead_url_reasons_for_lifecycle(self) -> None:
        for reason in (ALDI_UNAVAILABLE_REASON, ALDI_GONE_REASON):
            with self.subTest(reason=reason):
                self.assertEqual(classify_dead_reason(f"Product removed: {reason}"), 'discontinued')


if __name__ == '__main__':
    unittest.main()
