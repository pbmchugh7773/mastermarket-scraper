"""
Guards shared by the Apify-based scrapers (Tesco, Dunnes).

Added after the 2026-09-04 scraper review:

* Credit preflight — Tesco's actor refused to launch on 6 Aug and 3 Sep 2026
  with "By launching this job you will exceed your remaining usage of $0.002".
  Checking GET /v2/users/me/limits first turns that into an explicit,
  early failure that also tells you when the monthly cycle resets.
* Coverage check — on 31 Aug 2026 the Tesco actor finished SUCCEEDED with
  803 of 1810 URLs and the script counted it as a clean run. Comparing
  results against URLs sent makes a partial run visible (and, in the
  callers, a non-zero exit).

Pure functions here; network access lives in fetch_limits() only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

APIFY_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"

# Env knobs (read by the callers, documented here so both scrapers agree).
MIN_REMAINING_USD_ENV = "APIFY_MIN_REMAINING_USD"   # default 0.75
MIN_RESULT_PCT_ENV = "APIFY_MIN_RESULT_PCT"         # default 90
DEFAULT_MIN_REMAINING_USD = 0.75
DEFAULT_MIN_RESULT_PCT = 90.0


@dataclass
class CreditStatus:
    ok: bool
    remaining_usd: Optional[float]
    max_usd: Optional[float]
    used_usd: Optional[float]
    cycle_end: Optional[str]
    message: str


def fetch_limits(token: str, timeout: int = 20) -> dict:
    """GET /v2/users/me/limits for the account behind `token`."""
    resp = requests.get(
        APIFY_LIMITS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


def evaluate_credit(limits: dict, min_remaining_usd: float) -> CreditStatus:
    """
    Decide whether an actor launch is affordable.

    Accepts either the raw API response ({"data": {...}}) or the unwrapped
    data dict (what apify_client's UserClient.limits() returns). A payload
    whose numbers cannot be read never blocks a run — it is reported as
    unreadable and the caller proceeds.
    """
    data = limits.get("data", limits) if isinstance(limits, dict) else {}
    try:
        max_usd = float(data["limits"]["maxMonthlyUsageUsd"])
        used_usd = float(data["current"]["monthlyUsageUsd"])
    except (KeyError, TypeError, ValueError):
        return CreditStatus(
            ok=True, remaining_usd=None, max_usd=None, used_usd=None, cycle_end=None,
            message="Apify credit: could not read limits payload — proceeding without preflight",
        )
    cycle_end = (data.get("monthlyUsageCycle") or {}).get("endAt")
    remaining = max_usd - used_usd
    cycle_txt = f", cycle resets {cycle_end[:10]}" if cycle_end else ""
    if remaining < min_remaining_usd:
        return CreditStatus(
            ok=False, remaining_usd=remaining, max_usd=max_usd, used_usd=used_usd, cycle_end=cycle_end,
            message=(
                f"Apify credit: insufficient credits — ${remaining:.3f} remaining of "
                f"${max_usd:.2f} (need ${min_remaining_usd:.2f}{cycle_txt}). "
                "Top up at https://console.apify.com/billing/subscription or wait for the reset."
            ),
        )
    return CreditStatus(
        ok=True, remaining_usd=remaining, max_usd=max_usd, used_usd=used_usd, cycle_end=cycle_end,
        message=f"Apify credit: ${remaining:.3f} remaining of ${max_usd:.2f}{cycle_txt}",
    )


def evaluate_coverage(sent: int, got: int, min_pct: float) -> Tuple[bool, float, str]:
    """(ok, pct, message) — did the actor return enough results for the URLs sent?"""
    if sent <= 0:
        return True, 100.0, "Coverage: nothing sent to Apify"
    pct = got * 100.0 / sent
    if pct < min_pct:
        return (
            False, pct,
            f"PARTIAL RUN: Apify returned {got} results for {sent} URLs "
            f"({pct:.1f}% < {min_pct:.0f}%). Prices found were still uploaded; "
            "check the actor run in the Apify console.",
        )
    return True, pct, f"Coverage: {got}/{sent} results ({pct:.1f}%)"


def min_remaining_usd_from_env() -> float:
    return float(os.getenv(MIN_REMAINING_USD_ENV, DEFAULT_MIN_REMAINING_USD))


def min_result_pct_from_env() -> float:
    return float(os.getenv(MIN_RESULT_PCT_ENV, DEFAULT_MIN_RESULT_PCT))
