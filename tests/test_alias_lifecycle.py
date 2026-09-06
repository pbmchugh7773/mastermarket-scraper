"""
Dead-alias lifecycle (2026-09 review, P1):

* scrape failures reach the backend with a specific reason (HTTP 404 instead
  of the generic "Failed to extract price"),
* an alias that is dead on two different scrape days is marked unavailable
  automatically via PATCH /api/product-aliases/{id}/mark-unavailable,
* get_pending_aliases logs the API's total_pending and never returns the
  same alias twice (the API ignores `offset`, so pages repeat).
"""
from __future__ import annotations

import logging
import pathlib
import sys
from datetime import date

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import simple_local_to_prod as slp  # noqa: E402
from alias_lifecycle import classify_dead_reason, should_mark_unavailable  # noqa: E402


# --- pure rules ----------------------------------------------------------------

@pytest.mark.parametrize("message, preset", [
    ("HTTP 404", "persistent_404"),
    ("HTTP 410", "persistent_404"),
    ("Product removed: Product no longer available on SuperValu online store", "discontinued"),
    ("Redirected to non-PDP page: https://www.aldi.ie/", "redirected_to_category"),
    ("Failed to extract price", None),
    ("HTTP 503", None),
    ("", None),
    (None, None),
])
def test_classify_dead_reason(message, preset):
    assert classify_dead_reason(message) == preset


THU = "2026-09-03T08:33:05+00:00"
TODAY = date(2026, 9, 7)


def _alias(**over):
    base = {"id": 770, "last_scrape_success": False, "scrape_error_message": "HTTP 404", "last_scraped_at": THU}
    base.update(over)
    return base


def test_marks_when_dead_on_two_different_days():
    assert should_mark_unavailable(_alias(), "HTTP 404", TODAY) == "persistent_404"


def test_preset_follows_the_current_failure():
    assert should_mark_unavailable(_alias(), "Redirected to non-PDP page: https://x/", TODAY) == "redirected_to_category"


def test_not_when_previous_failure_was_today():
    assert should_mark_unavailable(_alias(last_scraped_at="2026-09-07T04:20:00+00:00"), "HTTP 404", TODAY) is None


def test_not_when_previous_attempt_succeeded():
    assert should_mark_unavailable(_alias(last_scrape_success=True), "HTTP 404", TODAY) is None


def test_not_when_previous_error_was_not_a_dead_url():
    assert should_mark_unavailable(_alias(scrape_error_message="Failed to extract price"), "HTTP 404", TODAY) is None


def test_not_when_current_failure_is_not_a_dead_url():
    assert should_mark_unavailable(_alias(), "Failed to extract price", TODAY) is None


def test_not_without_tracking_fields():
    assert should_mark_unavailable({"id": 1}, "HTTP 404", TODAY) is None


def test_accepts_zulu_timestamps():
    assert should_mark_unavailable(_alias(last_scraped_at="2026-09-03T08:33:05Z"), "HTTP 404", TODAY) == "persistent_404"


# --- scraper integration -------------------------------------------------------

class _Resp:
    def __init__(self, status_code, url, payload=None, text=""):
        self.status_code = status_code
        self.url = url
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _bare_scraper():
    s = slp.SimpleLocalScraper.__new__(slp.SimpleLocalScraper)
    s._last_redirect_error = None
    s._last_failure_reason = None
    s.auto_mark_unavailable = True
    return s


def test_lidl_404_records_http_status_as_failure_reason(monkeypatch):
    url = "https://www.lidl.ie/p/potato-gratin/p10000681"
    monkeypatch.setattr(slp.requests, "get", lambda u, **kw: _Resp(404, u))
    scraper = _bare_scraper()
    assert scraper.scrape_lidl(url, "Potato Gratin") is None
    assert scraper._last_failure_reason == "HTTP 404"


def test_maybe_mark_unavailable_patches_backend_when_rule_matches(monkeypatch):
    scraper = _bare_scraper()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return _Resp(200, url, {"alias": {"id": 770}, "hidden_prices_count": 3})

    monkeypatch.setattr(scraper, "_authed_request", fake_request)
    monkeypatch.setattr(slp, "_today", lambda: TODAY)
    assert scraper._maybe_mark_unavailable(_alias(), "HTTP 404") is True
    assert calls == [("patch", f"{slp.API_URL}/api/product-aliases/770/mark-unavailable", {"reason": "persistent_404"})]


def test_maybe_mark_unavailable_is_a_noop_when_rule_does_not_match(monkeypatch):
    scraper = _bare_scraper()
    monkeypatch.setattr(scraper, "_authed_request", lambda *a, **k: pytest.fail("must not call API"))
    monkeypatch.setattr(slp, "_today", lambda: TODAY)
    assert scraper._maybe_mark_unavailable(_alias(last_scrape_success=True), "HTTP 404") is False


def test_maybe_mark_unavailable_respects_opt_out(monkeypatch):
    scraper = _bare_scraper()
    scraper.auto_mark_unavailable = False
    monkeypatch.setattr(scraper, "_authed_request", lambda *a, **k: pytest.fail("must not call API"))
    monkeypatch.setattr(slp, "_today", lambda: TODAY)
    assert scraper._maybe_mark_unavailable(_alias(), "HTTP 404") is False


class _FakeSession:
    """API that ignores `offset` — every page is the same first 1000 rows."""

    def __init__(self, rows, total_pending):
        self.rows = rows
        self.total_pending = total_pending
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        page = self.rows[: params["limit"]]
        return _Resp(200, url, {"aliases": page, "total_pending": self.total_pending})


def test_get_pending_aliases_dedupes_and_logs_total_pending(caplog):
    scraper = _bare_scraper()
    scraper.session = _FakeSession([{"id": i} for i in range(1, 1001)], total_pending=1734)
    with caplog.at_level(logging.INFO):
        got = scraper.get_pending_aliases("SuperValu", limit=1500)
    ids = [a["id"] for a in got]
    assert len(ids) == len(set(ids)) == 1000
    assert "total pending: 1734" in caplog.text
