"""
Dunnes online-catalogue gate (2026-10-16): Apify kept publishing prices for
134 products Dunnes no longer sells online. The storefront API 404s them.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import dunnes_catalog as dc  # noqa: E402


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Session:
    """Answers per store in CHECK_STORES order; an Exception entry is raised."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        status = self.statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        return _Resp(status)


@pytest.mark.parametrize("url, sku", [
    ("https://www.dunnesstoresgrocery.com/product/options-instant-belgian-hot-chocolate-drink-11g-id-100664947", "100664947"),
    ("https://www.dunnesstoresgrocery.com/sm/delivery/rsid/258/product/knorr-stock-pots-chicken-8x-28-g--id-100795064", "100795064"),
    ("https://www.dunnesstoresgrocery.com/product/kefir-milk", None),
    (None, None),
])
def test_dunnes_sku(url, sku):
    assert dc.dunnes_sku(url) == sku


def test_listed_at_the_first_store_stops_early():
    session = _Session([200])
    assert dc.catalogue_status("100715797", session=session) == "listed"
    assert len(session.calls) == 1 and "/stores/258/products/100715797" in session.calls[0]


def test_listed_at_another_store_is_listed():
    assert dc.catalogue_status("1", session=_Session([404, 200])) == "listed"


def test_missing_needs_a_404_at_every_store():
    session = _Session([404, 404, 404])
    assert dc.catalogue_status("100664947", session=session) == "missing"
    assert [c.split("/stores/")[1].split("/")[0] for c in session.calls] == list(dc.CHECK_STORES)


@pytest.mark.parametrize("statuses", [
    [404, 500],
    [503],
    [requests.ConnectionError("down"), requests.ConnectionError("down")],
])
def test_errors_are_unknown_so_callers_fail_open(statuses, monkeypatch):
    monkeypatch.setattr(dc.time, "sleep", lambda s: None)
    assert dc.catalogue_status("1", session=_Session(statuses)) == "unknown"


def test_no_sku_is_unknown():
    assert dc.catalogue_status(None) == "unknown"


def test_missing_message_is_a_dead_url_reason():
    from alias_lifecycle import classify_dead_reason

    assert classify_dead_reason(dc.MISSING_MESSAGE) == "persistent_404"


# --- Apify Dunnes scraper gate -------------------------------------------------

def _scraper(monkeypatch, statuses_by_url):
    import apify_dunnes_scraper as ads

    s = ads.ApifyDunnesScraper.__new__(ads.ApifyDunnesScraper)
    s.dry_run = False
    s.stats = {"not_in_catalogue": 0}
    s.status_calls, s.patch_calls = [], []
    s.update_scraping_status = lambda **kw: s.status_calls.append(kw) or True

    class _S:
        def patch(self, url, **kw):
            s.patch_calls.append((url, kw.get("json")))
            return _Resp(200, {"hidden_prices_count": 30})

    s.session = _S()
    monkeypatch.setattr(ads, "catalogue_status", lambda sku: statuses_by_url[sku])
    monkeypatch.setattr(ads.time, "sleep", lambda x: None)
    return s


def test_gate_drops_and_retires_missing_products(monkeypatch):
    listed = "https://www.dunnesstoresgrocery.com/product/glenisk-id-100715797"
    missing = "https://www.dunnesstoresgrocery.com/product/options-id-100664947"
    unknown = "https://www.dunnesstoresgrocery.com/product/kefir-milk"
    s = _scraper(monkeypatch, {"100715797": "listed", "100664947": "missing", None: "unknown"})
    aliases = {missing: {"id": 2246, "alias_name": "Options"}}
    kept = s.drop_missing_from_catalogue([listed, missing, unknown], aliases)
    assert kept == [listed, unknown]
    assert s.stats["not_in_catalogue"] == 1
    assert s.status_calls == [{"alias_id": 2246, "success": False, "error_message": dc.MISSING_MESSAGE}]
    assert s.patch_calls == [(f"{__import__('apify_dunnes_scraper').API_URL}/api/product-aliases/2246/mark-unavailable",
                              {"reason": "persistent_404"})]


def test_gate_dry_run_writes_nothing_but_still_skips(monkeypatch):
    s = _scraper(monkeypatch, {"100664947": "missing"})
    s.dry_run = True
    url = "https://www.dunnesstoresgrocery.com/product/options-id-100664947"
    assert s.drop_missing_from_catalogue([url], {url: {"id": 2246}}) == []
    assert s.patch_calls == []


# --- re-probe ------------------------------------------------------------------

def test_reprobe_dunnes_uses_the_catalogue(monkeypatch):
    import reprobe_unavailable_aliases as rp

    alias = {"scraper_url": "https://www.dunnesstoresgrocery.com/product/options-id-100664947", "alias_name": "Options"}
    scraper = type("S", (), {"_last_redirect_error": None, "_last_failure_reason": None})()
    monkeypatch.setattr(rp, "catalogue_status", lambda sku: "listed")
    assert rp.probe(scraper, "Dunnes Stores", alias) == ("alive", None)
    monkeypatch.setattr(rp, "catalogue_status", lambda sku: "missing")
    assert rp.probe(scraper, "Dunnes Stores", alias) == ("dead (HTTP 404)", None)
    monkeypatch.setattr(rp, "catalogue_status", lambda sku: "unknown")
    assert rp.probe(scraper, "Dunnes Stores", alias) == ("error", None)
