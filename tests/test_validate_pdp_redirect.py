"""
Regression tests for _validate_pdp_redirect / PDP_PATTERNS in
simple_local_to_prod.py.

Every URL below is copied from real scrape logs (2026-09-03 Price Scraping
runs), so the patterns are pinned to the URL shapes the stores actually use.
Background: PDP_PATTERNS['aldi'] was written as /p/<slug> while Aldi PDPs live
under /product/<slug>-<code>, so any slug redirect (same product code, new
slug) was rejected as "non-PDP" and the alias failed forever.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from simple_local_to_prod import _validate_pdp_redirect  # noqa: E402


ALDI_OLD = "https://www.aldi.ie/product/hellmanns-light-mayo-000000000443821002"
ALDI_NEW = "https://www.aldi.ie/product/hellmanns-light-mayonnaise-000000000443821002"
SV_RSID = "https://shop.supervalu.ie/sm/delivery/rsid/5550/product/obento-japanese-soy-sauce-250-ml-id-1356498000"
SV_PLAIN = "https://shop.supervalu.ie/product/obento-japanese-soy-sauce-250-ml-id-1356498000"


@pytest.mark.parametrize(
    "store, requested, final",
    [
        ("Aldi", ALDI_OLD, ALDI_NEW),                      # slug rename, same product code
        ("SuperValu", SV_RSID, SV_PLAIN),                  # store-scoped → canonical PDP
        ("SuperValu", SV_PLAIN, SV_RSID),                  # canonical → store-scoped PDP
        ("Tesco", "https://www.tesco.ie/groceries/en-IE/products/320403599",
                  "https://www.tesco.ie/groceries/en-IE/products/320403599?x=1"),
        ("Lidl", "https://www.lidl.ie/p/potato-gratin/p10000681",
                 "https://www.lidl.ie/p/4-mini-potato-gratin/p10000681"),
        ("Dunnes Stores", "https://www.dunnesstores.com/p/batchelors-mushy-peas-100123456.html",
                          "https://www.dunnesstores.com/p/batchelors-mushy-peas-420g-100123456.html"),
    ],
)
def test_redirect_to_another_pdp_is_valid(store, requested, final):
    ok, err = _validate_pdp_redirect(store, requested, final)
    assert ok, err
    assert err is None


@pytest.mark.parametrize(
    "store, requested, final",
    [
        ("Aldi", ALDI_OLD, "https://www.aldi.ie/"),
        ("Aldi", ALDI_OLD, "https://www.aldi.ie/groceries"),
        ("SuperValu", SV_RSID, "https://shop.supervalu.ie/sm/delivery/rsid/5550"),
        ("SuperValu", SV_RSID, "https://shop.supervalu.ie/sm/delivery/rsid/5550/categories/food-cupboard-id-520436"),
        ("Tesco", "https://www.tesco.ie/groceries/en-IE/products/320403599",
                  "https://www.tesco.ie/groceries/en-IE/shop/food-cupboard/all"),
        ("Lidl", "https://www.lidl.ie/p/potato-gratin/p10000681", "https://www.lidl.ie/search?q=gratin"),
    ],
)
def test_redirect_to_non_pdp_is_rejected(store, requested, final):
    ok, err = _validate_pdp_redirect(store, requested, final)
    assert not ok
    assert err == f"Redirected to non-PDP page: {final}"


def test_no_redirect_is_valid_regardless_of_pattern():
    assert _validate_pdp_redirect("Aldi", ALDI_OLD, ALDI_OLD) == (True, None)


def test_trailing_slash_and_case_do_not_count_as_redirect():
    assert _validate_pdp_redirect("aldi", ALDI_OLD, ALDI_OLD.upper() + "/") == (True, None)


def test_missing_final_url_lets_caller_proceed():
    assert _validate_pdp_redirect("Aldi", ALDI_OLD, "") == (True, None)
