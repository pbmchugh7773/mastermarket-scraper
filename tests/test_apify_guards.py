"""
Tests for apify_guards.py — the two guards added after the 2026-09 review:

  * credit preflight: refuse to launch an actor when the Apify account has
    less than APIFY_MIN_REMAINING_USD left (Tesco died twice on
    "exceed your remaining usage of $0.002" — 6 Aug and 3 Sep 2026).
  * coverage check: an actor run that SUCCEEDED with 803 of 1810 URLs
    (31 Aug 2026) must be reported as a partial run, not a clean success.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from apify_guards import evaluate_coverage, evaluate_credit  # noqa: E402

LIMITS = {
    "data": {
        "monthlyUsageCycle": {"startAt": "2026-08-07T00:00:00.000Z", "endAt": "2026-09-07T00:00:00.000Z"},
        "limits": {"maxMonthlyUsageUsd": 5.0},
        "current": {"monthlyUsageUsd": 4.998},
    }
}


def test_credit_insufficient_when_remaining_below_threshold():
    status = evaluate_credit(LIMITS, min_remaining_usd=0.75)
    assert status.ok is False
    assert status.remaining_usd == pytest.approx(0.002)
    assert "insufficient credits" in status.message
    assert "2026-09-07" in status.message  # cycle reset date is surfaced


def test_credit_ok_when_enough_remaining():
    limits = {"data": {**LIMITS["data"], "current": {"monthlyUsageUsd": 3.0}}}
    status = evaluate_credit(limits, min_remaining_usd=0.75)
    assert status.ok is True
    assert status.remaining_usd == pytest.approx(2.0)
    assert "2026-09-07" in status.message


def test_credit_accepts_unwrapped_data_dict():
    status = evaluate_credit(LIMITS["data"], min_remaining_usd=0.75)
    assert status.ok is False


def test_credit_unknown_shape_does_not_block():
    status = evaluate_credit({"data": {"limits": {}}}, min_remaining_usd=0.75)
    assert status.ok is True
    assert status.remaining_usd is None
    assert "could not read" in status.message


def test_coverage_partial_below_threshold():
    ok, pct, message = evaluate_coverage(sent=1810, got=803, min_pct=90)
    assert ok is False
    assert pct == pytest.approx(44.4, abs=0.1)
    assert "PARTIAL RUN" in message and "803" in message and "1810" in message


def test_coverage_ok_above_threshold():
    ok, pct, _ = evaluate_coverage(sent=1810, got=1796, min_pct=90)
    assert ok is True
    assert pct == pytest.approx(99.2, abs=0.1)


def test_coverage_nothing_sent_is_ok():
    ok, pct, _ = evaluate_coverage(sent=0, got=0, min_pct=90)
    assert ok is True
    assert pct == 100.0


# --- wrapper behaviour inside the two scrapers -------------------------------

@pytest.fixture(autouse=True)
def _apify_env(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")
    monkeypatch.delenv("APIFY_MIN_REMAINING_USD", raising=False)


def _bare(cls):
    obj = cls.__new__(cls)
    obj.stats = {}
    return obj


@pytest.mark.parametrize("module_name, class_name", [
    ("apify_tesco_scraper", "ApifyTescoScraper"),
    ("apify_dunnes_scraper", "ApifyDunnesScraper"),
])
def test_check_apify_credit_raises_when_insufficient(monkeypatch, module_name, class_name):
    mod = __import__(module_name)
    monkeypatch.setattr(mod, "fetch_limits", lambda token: LIMITS)
    scraper = _bare(getattr(mod, class_name))
    with pytest.raises(RuntimeError, match="insufficient credits"):
        scraper.check_apify_credit()
    assert scraper.stats["apify_remaining_usd"] == pytest.approx(0.002)


@pytest.mark.parametrize("module_name, class_name", [
    ("apify_tesco_scraper", "ApifyTescoScraper"),
    ("apify_dunnes_scraper", "ApifyDunnesScraper"),
])
def test_check_apify_credit_skips_when_limits_unreachable(monkeypatch, module_name, class_name):
    mod = __import__(module_name)

    def boom(token):
        raise ConnectionError("apify down")

    monkeypatch.setattr(mod, "fetch_limits", boom)
    scraper = _bare(getattr(mod, class_name))
    scraper.check_apify_credit()  # must not raise
    assert "apify_remaining_usd" not in scraper.stats
