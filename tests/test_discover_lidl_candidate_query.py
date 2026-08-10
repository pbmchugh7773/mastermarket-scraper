"""
Regression tests for the API-backed candidate query in discover_lidl_aliases.

Context: the weekly "Discover Lidl Aliases" workflow failed 7/7 runs with
FileNotFoundError because query_candidate_products() shelled out to
`/home/<dev>/projects/MasterMarket/scripts/query_prod.sh`, a path that only
exists on a developer laptop. The subprocess call was replaced by
/products/all-simple + /api/product-aliases/store/{store}, with the filtering
logic extracted into the pure select_candidate_products().

These tests pin that filter to the semantics of the SQL it replaced. They are
pure-Python and do NOT touch the DB or the network.
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
    select_candidate_products,
)


def _product(pid: int, name: str, brand, unit: str = "g"):
    """A row shaped like /products/all-simple returns it."""
    return {"id": pid, "name": name, "brand": brand, "unit": unit}


class SelectCandidateProductsOwnBrandTests(unittest.TestCase):
    """pool=lidl-own-brand — the pool the weekly workflow actually runs."""

    def test_keeps_lidl_exclusive_brand_without_lidl_alias(self):
        products = [_product(1, "Barista Oat Milk", "Vemondo")]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [1])

    def test_drops_product_that_already_has_a_lidl_alias(self):
        products = [_product(1, "Barista Oat Milk", "Vemondo")]
        out = select_candidate_products(products, {1}, set(), POOL_LIDL_OWN)
        self.assertEqual(out, [])

    def test_brand_match_is_substring_and_case_insensitive(self):
        """DB rows vary: "Milbona", "Lidl, Milbona", "Bio Organic, Lidl, Milbona"."""
        products = [
            _product(1, "Greek Yoghurt", "Milbona"),
            _product(2, "Greek Yoghurt", "Lidl, Milbona"),
            _product(3, "Greek Yoghurt", "Bio Organic, Lidl, MILBONA"),
        ]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [1, 2, 3])

    def test_drops_non_lidl_brands(self):
        products = [
            _product(1, "Greek Yoghurt", "Alpro"),
            _product(2, "Greek Yoghurt", "Milbona"),
        ]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [2])

    def test_drops_null_and_blank_brands(self):
        """Mirrors `p.brand IS NOT NULL AND p.brand <> ''`."""
        products = [
            _product(1, "Mystery Item", None),
            _product(2, "Mystery Item", ""),
            _product(3, "Mystery Item", "   "),
            _product(4, "Greek Yoghurt", "Milbona"),
        ]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [4])

    def test_own_brand_pool_ignores_aldi_alias_state(self):
        """Lidl exclusives have no Aldi cross-listing, so Aldi must not gate them."""
        products = [_product(1, "Greek Yoghurt", "Milbona")]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [1])


class SelectCandidateProductsCrossListTests(unittest.TestCase):
    """pool=aldi-cross-list — v2 behaviour, preserved."""

    def test_requires_an_aldi_alias(self):
        products = [
            _product(1, "Ketchup", "Heinz"),
            _product(2, "Ketchup", "Heinz"),
        ]
        out = select_candidate_products(products, set(), {2}, POOL_ALDI)
        self.assertEqual([p["id"] for p in out], [2])

    def test_excludes_store_own_brands(self):
        products = [
            _product(1, "Beans", "Aldi"),
            _product(2, "Beans", "Tesco"),
            _product(3, "Beans", "Dunnes Stores"),
            _product(4, "Beans", "Heinz"),
        ]
        aldi = {1, 2, 3, 4}
        out = select_candidate_products(products, set(), aldi, POOL_ALDI)
        self.assertEqual([p["id"] for p in out], [4])

    def test_excludes_aldi_sub_brand_substrings(self):
        """Mirrors NOT ILIKE '%aldi%' / '%specially selected%' / '%simply%'."""
        products = [
            _product(1, "Steak", "Specially Selected"),
            _product(2, "Bread", "Simply Nature"),
            _product(3, "Crisps", "ALDI Ireland"),
            _product(4, "Ketchup", "Heinz"),
        ]
        aldi = {1, 2, 3, 4}
        out = select_candidate_products(products, set(), aldi, POOL_ALDI)
        self.assertEqual([p["id"] for p in out], [4])

    def test_drops_product_that_already_has_a_lidl_alias(self):
        products = [_product(1, "Ketchup", "Heinz")]
        out = select_candidate_products(products, {1}, {1}, POOL_ALDI)
        self.assertEqual(out, [])


class SelectCandidateProductsShapeTests(unittest.TestCase):
    def test_result_is_ordered_by_product_id(self):
        """Mirrors `ORDER BY p.id` — /products/all-simple is not id-sorted."""
        products = [
            _product(30, "Greek Yoghurt", "Milbona"),
            _product(10, "Pizza", "Combino"),
            _product(20, "Oat Milk", "Vemondo"),
        ]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual([p["id"] for p in out], [10, 20, 30])

    def test_emits_keys_the_matching_phase_consumes(self):
        products = [_product(1, "Barista Oat Milk 1L", "Vemondo", unit="l")]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual(
            set(out[0]),
            {"id", "name", "brand", "unit", "norm", "size", "variant"},
        )

    def test_null_name_and_unit_do_not_crash(self):
        """COALESCE(p.unit,'') had a Python equivalent; keep it."""
        products = [{"id": 1, "name": None, "brand": "Milbona", "unit": None}]
        out = select_candidate_products(products, set(), set(), POOL_LIDL_OWN)
        self.assertEqual(out[0]["name"], "")
        self.assertEqual(out[0]["unit"], "")

    def test_rejects_unknown_pool(self):
        with self.assertRaises(ValueError):
            select_candidate_products([], set(), set(), "not-a-pool")


if __name__ == "__main__":
    unittest.main()
