"""
Dead-alias lifecycle rules for the price scrapers (2026-09 review, P1).

A scraper failure is a *dead-URL* failure when the store itself says the
product page is gone: HTTP 404/410, a "product removed" page, or a redirect
to a non-PDP page. Those aliases sat at the front of every retry batch for
weeks (SuperValu: 202 of 700 slots per batch, Lidl: 77 of 77) because nothing
ever took them out of rotation.

Rule: an alias whose *previous* attempt (on an earlier scrape day) was a
dead-URL failure and whose *current* attempt is a dead-URL failure again is
marked unavailable via PATCH /api/product-aliases/{id}/mark-unavailable with
the admin-UI preset that matches the current failure. Two different days —
not two batches of the same day — so a transient blip never retires an alias.
Anything else (timeouts, parse failures, 5xx) is left alone.

Pure functions only; the HTTP call lives in the scraper.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

# (pattern, admin-UI preset from MarkAliasUnavailableRequest)
DEAD_URL_PATTERNS = (
    (re.compile(r"^HTTP (404|410)\b"), "persistent_404"),
    (re.compile(r"^Product removed\b"), "discontinued"),
    (re.compile(r"^Redirected to non-PDP page\b"), "redirected_to_category"),
)


def classify_dead_reason(error_message: Optional[str]) -> Optional[str]:
    """Preset name if `error_message` describes a dead URL, else None."""
    if not error_message:
        return None
    for pattern, preset in DEAD_URL_PATTERNS:
        if pattern.search(error_message):
            return preset
    return None


def _scrape_day(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def should_mark_unavailable(alias: dict, error_message_now: Optional[str], today: date) -> Optional[str]:
    """
    Preset to mark `alias` unavailable with, or None to leave it alone.

    Requires: current failure is dead-URL; previous attempt failed with a
    dead-URL reason; previous attempt happened on an earlier day than `today`.
    """
    preset_now = classify_dead_reason(error_message_now)
    if preset_now is None:
        return None
    if alias.get("last_scrape_success") is not False:
        return None
    if classify_dead_reason(alias.get("scrape_error_message")) is None:
        return None
    previous_day = _scrape_day(alias.get("last_scraped_at"))
    if previous_day is None or previous_day >= today:
        return None
    return preset_now
