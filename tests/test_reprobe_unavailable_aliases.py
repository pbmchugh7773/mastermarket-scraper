"""
reprobe_unavailable_aliases.py (2026-10-15): retired aliases are re-checked
and restored when the store page has a price again — 115 of 349 SuperValu
retirements were live products on 2026-10-14.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import reprobe_unavailable_aliases as rp  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _retired(alias_id, reason="discontinued", **over):
    base = {
        "id": alias_id, "product_id": 1000 + alias_id, "alias_name": f"Product {alias_id}",
        "scraper_url": f"https://shop.supervalu.ie/product/p-id-{alias_id}",
        "is_unavailable": True, "unavailable_reason": reason, "unavailable_at": "2026-10-05T08:00:00Z",
    }
    base.update(over)
    return base


class _FakeScraper:
    """Store pages keyed by URL: a price, 'removed', 'redirect' or 'http404'."""

    def __init__(self, aliases, pages):
        self.aliases = aliases
        self.pages = pages
        self.driver = None
        self.calls = []
        self.statuses = []
        self._last_redirect_error = None
        self._last_failure_reason = None

    def _authed_request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == "get":
            return _Resp(200, self.aliases)
        return _Resp(200, {})

    def update_scraping_status(self, **kwargs):
        self.statuses.append(kwargs)
        return True

    def scrape_supervalu(self, url, name):
        page = self.pages[url]
        if page == "removed":
            return None, {"product_removed": True, "reason": "gone"}
        if page == "redirect":
            self._last_redirect_error = "Redirected to non-PDP page: https://shop.supervalu.ie/"
            return None, None
        if page == "http404":
            self._last_failure_reason = "HTTP 404"
            return None, None
        return page, None


@pytest.mark.parametrize("over, expected", [
    ({}, True),
    ({"unavailable_reason": "persistent_404"}, True),
    ({"unavailable_reason": "redirected_to_category"}, True),
    ({"unavailable_reason": "manual_admin"}, False),
    ({"unavailable_reason": "other"}, False),
    ({"is_unavailable": False}, False),
    ({"scraper_url": None}, False),
])
def test_is_reprobe_candidate(over, expected):
    assert rp.is_reprobe_candidate(_retired(1, **over)) is expected


def test_read_alias_ids_accepts_csv_or_plain_lines(tmp_path):
    csv_file = tmp_path / "ids.csv"
    csv_file.write_text("alias_id,retired_on\n5119,2026-10-05\n1270,2026-10-04\n")
    plain = tmp_path / "ids.txt"
    plain.write_text("5119\n1270\n\n")
    assert rp.read_alias_ids(csv_file) == {5119, 1270}
    assert rp.read_alias_ids(plain) == {5119, 1270}


def _world():
    aliases = [
        _retired(1),                                  # back on sale
        _retired(2),                                  # still "Storefront EN"
        _retired(3, reason="persistent_404"),         # still 404
        _retired(4, reason="redirected_to_category"), # still redirects
        _retired(5, reason="manual_admin"),           # human call — never touched
        _retired(6, is_unavailable=False),            # active alias
    ]
    pages = {a["scraper_url"]: page for a, page in zip(aliases, [2.79, "removed", "http404", "redirect", 9.99, 1.0])}
    return aliases, pages


def test_proposal_mode_probes_but_writes_nothing():
    aliases, pages = _world()
    scraper = _FakeScraper(aliases, pages)
    rows = rp.reprobe(scraper, ["SuperValu"], apply=False, delay=0)
    assert [(r["alias_id"], r["verdict"]) for r in rows] == [
        (1, "alive"), (2, "still_removed"), (3, "dead (HTTP 404)"), (4, "redirected"),
    ]
    assert rows[0]["live_price"] == "2.79"
    assert not any(r["restored"] for r in rows)
    assert [m for m, _ in scraper.calls] == ["get"]
    assert scraper.statuses == []


def test_apply_restores_only_aliases_with_a_price_and_clears_their_status():
    aliases, pages = _world()
    scraper = _FakeScraper(aliases, pages)
    rows = rp.reprobe(scraper, ["SuperValu"], apply=True, delay=0)
    assert [r["alias_id"] for r in rows if r["restored"]] == [1]
    assert ("patch", f"{rp.slp.API_URL}/api/product-aliases/1/mark-available") in scraper.calls
    assert sum(1 for m, _ in scraper.calls if m == "patch") == 1
    assert scraper.statuses == [{"alias_id": 1, "success": True, "price": 2.79}]


def test_only_ids_restricts_the_probe():
    aliases, pages = _world()
    scraper = _FakeScraper(aliases, pages)
    rows = rp.reprobe(scraper, ["SuperValu"], apply=False, only_ids={2, 3, 5}, delay=0)
    assert [r["alias_id"] for r in rows] == [2, 3]


def test_failed_mark_available_is_not_counted_as_restored():
    aliases, pages = _world()
    scraper = _FakeScraper(aliases, pages)
    scraper._authed_request = lambda method, url, **kw: _Resp(200, aliases) if method == "get" else _Resp(500, text="boom")
    rows = rp.reprobe(scraper, ["SuperValu"], apply=True, delay=0)
    assert not any(r["restored"] for r in rows)


def test_status_is_cleared_before_mark_available():
    # A timeout the server did process must not leave the alias back in the
    # pool with its old dead-URL streak; clearing first makes a retry safe.
    aliases, pages = _world()
    scraper = _FakeScraper(aliases, pages)
    order = []
    scraper.update_scraping_status = lambda **kw: order.append("status") or True
    real = scraper._authed_request
    scraper._authed_request = lambda method, url, **kw: order.append(method) or real(method, url, **kw)
    rp.reprobe(scraper, ["SuperValu"], apply=True, delay=0)
    assert order == ["get", "status", "patch"]


def test_a_timeout_on_one_alias_does_not_abort_the_run():
    aliases, pages = _world()
    aliases.append(_retired(7))
    pages[aliases[-1]["scraper_url"]] = 3.50
    scraper = _FakeScraper(aliases, pages)
    real = scraper._authed_request

    def flaky(method, url, **kw):
        if method == "patch" and url.endswith("/1/mark-available"):
            raise TimeoutError("read timed out")
        return real(method, url, **kw)

    scraper._authed_request = flaky
    rows = rp.reprobe(scraper, ["SuperValu"], apply=True, delay=0)
    assert {r["alias_id"]: r["restored"] for r in rows if r["verdict"] == "alive"} == {1: False, 7: True}


def test_unknown_store_is_rejected():
    with pytest.raises(SystemExit):
        rp.main(["--stores", "Tesco"])
