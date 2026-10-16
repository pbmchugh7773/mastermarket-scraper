"""
Dead-alias lifecycle (2026-09 review, P1; streak rule 2026-10-15):

* scrape failures reach the backend with a specific reason (HTTP 404 instead
  of the generic "Failed to extract price"),
* dead-URL failures carry the start of their streak ("(dead since …)"), and an
  alias is marked unavailable via PATCH /api/product-aliases/{id}/mark-unavailable
  only after MIN_DEAD_STREAK_DAYS of dead attempts — "dead on two different
  days" retired 115 SuperValu products that were only briefly off the shelf,
* a success in any mode clears a previous failure, so a streak never spans it,
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
from alias_lifecycle import (  # noqa: E402
    AUTO_RETIRE_PRESETS,
    MIN_DEAD_STREAK_DAYS,
    annotate_dead_streak,
    classify_dead_reason,
    dead_streak_start,
    should_mark_unavailable,
)


# --- pure rules ----------------------------------------------------------------

@pytest.mark.parametrize("message, preset", [
    ("HTTP 404", "persistent_404"),
    ("HTTP 410", "persistent_404"),
    ("HTTP 404 (dead since 2026-10-01)", "persistent_404"),
    ("Product removed: Product no longer available on SuperValu online store", "discontinued"),
    ("Product removed: x (dead since 2026-10-01)", "discontinued"),
    ("Redirected to non-PDP page: https://www.aldi.ie/", "redirected_to_category"),
    ("Failed to extract price", None),
    ("HTTP 503", None),
    ("", None),
    (None, None),
])
def test_classify_dead_reason(message, preset):
    assert classify_dead_reason(message) == preset


def test_auto_retire_presets_are_exactly_the_lifecycle_presets():
    assert AUTO_RETIRE_PRESETS == {"persistent_404", "discontinued", "redirected_to_category"}


THU = "2026-09-03T08:33:05+00:00"
TODAY = date(2026, 9, 10)  # Thursday, one week after THU


def _alias(**over):
    base = {"id": 770, "last_scrape_success": False, "scrape_error_message": "HTTP 404 (dead since 2026-09-03)", "last_scraped_at": THU}
    base.update(over)
    return base


def test_marks_after_a_full_week_of_dead_attempts():
    assert MIN_DEAD_STREAK_DAYS == 7
    assert should_mark_unavailable(_alias(), "HTTP 404", TODAY) == "persistent_404"


def test_streak_start_comes_from_the_message_not_the_last_attempt():
    # Last attempt was Monday, but the streak began the Thursday before.
    alias = _alias(last_scraped_at="2026-09-07T05:00:00+00:00")
    assert should_mark_unavailable(alias, "HTTP 404", TODAY) == "persistent_404"


def test_not_after_sunday_then_monday():
    # The 2026-09 rule retired here: dead Sunday, dead again Monday.
    alias = _alias(scrape_error_message="Product removed: x (dead since 2026-09-06)",
                   last_scraped_at="2026-09-06T09:00:00+00:00")
    assert should_mark_unavailable(alias, "Product removed: x", date(2026, 9, 7)) is None


def test_not_one_day_short_of_the_streak():
    alias = _alias(scrape_error_message="HTTP 404 (dead since 2026-09-04)")
    assert should_mark_unavailable(alias, "HTTP 404", TODAY) is None


def test_legacy_message_falls_back_to_the_last_attempt_day():
    old = _alias(scrape_error_message="HTTP 404", last_scraped_at="2026-09-03T08:33:05+00:00")
    recent = _alias(scrape_error_message="HTTP 404", last_scraped_at="2026-09-07T05:00:00+00:00")
    assert should_mark_unavailable(old, "HTTP 404", TODAY) == "persistent_404"
    assert should_mark_unavailable(recent, "HTTP 404", TODAY) is None


def test_preset_follows_the_current_failure():
    assert should_mark_unavailable(_alias(), "Redirected to non-PDP page: https://x/", TODAY) == "redirected_to_category"


def test_not_when_previous_attempt_succeeded():
    assert should_mark_unavailable(_alias(last_scrape_success=True), "HTTP 404", TODAY) is None


def test_not_when_previous_error_was_not_a_dead_url():
    assert should_mark_unavailable(_alias(scrape_error_message="Failed to extract price"), "HTTP 404", TODAY) is None


def test_not_when_current_failure_is_not_a_dead_url():
    assert should_mark_unavailable(_alias(), "Failed to extract price", TODAY) is None


def test_not_without_tracking_fields():
    assert should_mark_unavailable({"id": 1}, "HTTP 404", TODAY) is None


def test_accepts_zulu_timestamps():
    alias = _alias(scrape_error_message="HTTP 404", last_scraped_at="2026-09-03T08:33:05Z")
    assert should_mark_unavailable(alias, "HTTP 404", TODAY) == "persistent_404"


def test_dead_streak_start():
    assert dead_streak_start(_alias()) == date(2026, 9, 3)
    assert dead_streak_start(_alias(last_scrape_success=True)) is None
    assert dead_streak_start(_alias(last_scrape_success=None)) is None
    assert dead_streak_start(_alias(scrape_error_message="Failed to extract price")) is None


def test_annotate_starts_a_streak_today():
    fresh = {"id": 1, "last_scrape_success": True, "scrape_error_message": None, "last_scraped_at": THU}
    assert annotate_dead_streak(fresh, "HTTP 404", TODAY) == "HTTP 404 (dead since 2026-09-10)"


def test_annotate_keeps_the_running_streak_start():
    assert annotate_dead_streak(_alias(), "Product removed: gone", TODAY) == "Product removed: gone (dead since 2026-09-03)"


def test_annotate_does_not_stack_suffixes():
    msg = "HTTP 404 (dead since 2026-09-03)"
    assert annotate_dead_streak(_alias(), msg, TODAY) == msg


def test_annotate_leaves_other_failures_alone():
    assert annotate_dead_streak(_alias(), "Failed to extract price", TODAY) == "Failed to extract price"
    assert annotate_dead_streak(_alias(), None, TODAY) is None


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


# --- scrape_store loop ---------------------------------------------------------

def _loop_scraper(monkeypatch, alias, scrape_result):
    """Scraper wired for one SuperValu alias; records update-status calls."""
    scraper = _bare_scraper()
    scraper.driver = None
    statuses = []
    monkeypatch.setattr(scraper, "get_pending_aliases", lambda store, limit: [alias])
    monkeypatch.setattr(scraper, "get_product_aliases", lambda store, limit, pid=None: [alias])
    monkeypatch.setattr(scraper, "scrape_supervalu", lambda url, name: scrape_result)
    monkeypatch.setattr(scraper, "upload_price", lambda *a, **k: True)
    monkeypatch.setattr(scraper, "update_scraping_status", lambda **kw: statuses.append(kw) or True)
    monkeypatch.setattr(scraper, "_authed_request", lambda *a, **k: _Resp(200, a[1], {"hidden_prices_count": 0}))
    monkeypatch.setattr(slp.time, "sleep", lambda s: None)
    monkeypatch.setattr(slp, "_today", lambda: TODAY)
    return scraper, statuses


def _sv_alias(**over):
    base = _alias(alias_name="Kenco Rich Coffee", product_id=1270, store_name="SuperValu",
                  scraper_url="https://shop.supervalu.ie/product/kenco-rich-coffee-100-g-id-1")
    base.update(over)
    return base


def test_removed_product_is_reported_with_its_streak_start(monkeypatch):
    fresh = _sv_alias(last_scrape_success=True, scrape_error_message=None)
    removed = (None, {"product_removed": True, "reason": "Product no longer available on SuperValu online store"})
    scraper, statuses = _loop_scraper(monkeypatch, fresh, removed)
    scraper.scrape_store("SuperValu", max_products=1, retry_mode=True)
    assert statuses == [{
        "alias_id": 770,
        "success": False,
        "error_message": "Product removed: Product no longer available on SuperValu online store (dead since 2026-09-10)",
    }]


def test_success_in_the_main_pass_clears_a_previous_failure(monkeypatch):
    scraper, statuses = _loop_scraper(monkeypatch, _sv_alias(), (2.79, None))
    scraper.scrape_store("SuperValu", max_products=1, retry_mode=False)
    assert [s["success"] for s in statuses] == [True]


def test_success_in_the_main_pass_skips_the_status_call_when_nothing_to_clear(monkeypatch):
    scraper, statuses = _loop_scraper(monkeypatch, _sv_alias(last_scrape_success=True, scrape_error_message=None), (2.79, None))
    scraper.scrape_store("SuperValu", max_products=1, retry_mode=False)
    assert statuses == []
