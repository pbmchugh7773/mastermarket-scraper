"""
Dead-alias lifecycle rules for the price scrapers (2026-09 review, P1;
streak rule 2026-10-15).

A scraper failure is a *dead-URL* failure when the store itself says the
product page is gone: HTTP 404/410, a "product removed" page, or a redirect
to a non-PDP page. Those aliases sat at the front of every retry batch for
weeks (SuperValu: 202 of 700 slots per batch, Lidl: 77 of 77) because nothing
ever took them out of rotation.

Rule: an alias is marked unavailable via
PATCH /api/product-aliases/{id}/mark-unavailable (with the admin-UI preset
that matches the current failure) once it has been dead on every attempt for
at least MIN_DEAD_STREAK_DAYS. Anything else (timeouts, parse failures, 5xx)
is left alone, and any non-dead result ends the streak.

Why a streak and not "dead on two different days" (the 2026-09 rule): the
dead signals are not always permanent. SuperValu serves its "Storefront EN"
soft-404 for products that are only temporarily off the online shelf, and the
schedule has a Sunday scan followed by a Monday scan — 24 h apart. A live
re-check on 2026-10-14 found 115 of the 349 SuperValu aliases retired by the
two-day rule (33%) back on sale with a price.

The alias row only keeps the latest attempt, so the streak start rides along
in the error message itself: "Product removed: … (dead since 2026-10-15)".
Messages written before that suffix existed fall back to the last attempt's
date, which can only make the streak look shorter than it is.

Retirement is not final: reprobe_unavailable_aliases.py re-checks the aliases
this module retires and restores the ones that have a price again.

Pure functions only; the HTTP calls live in the scrapers.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

# (pattern, admin-UI preset from MarkAliasUnavailableRequest)
DEAD_URL_PATTERNS = (
    (re.compile(r"^HTTP (404|410)\b"), "persistent_404"),
    (re.compile(r"^Product removed\b"), "discontinued"),
    (re.compile(r"^Redirected to non-PDP page\b"), "redirected_to_category"),
)

# Presets this module (and only this module) produces. The re-probe restores
# aliases retired with one of these; 'manual_admin' / 'other' are a human's
# call and are never touched automatically.
AUTO_RETIRE_PRESETS = frozenset(preset for _, preset in DEAD_URL_PATTERNS)

# A Thursday-dead alias is retired the following Thursday at the earliest,
# after failing the Sunday and Monday scans in between.
MIN_DEAD_STREAK_DAYS = 7

DEAD_SINCE_RE = re.compile(r"\s*\(dead since (\d{4}-\d{2}-\d{2})\)$")


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


def dead_streak_start(alias: dict) -> Optional[date]:
    """
    Day the alias's current dead-URL streak began, or None when its previous
    attempt was not a dead-URL failure (so a dead result now starts a streak).
    """
    if alias.get("last_scrape_success") is not False:
        return None
    message = alias.get("scrape_error_message")
    if classify_dead_reason(message) is None:
        return None
    match = DEAD_SINCE_RE.search(message)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    return _scrape_day(alias.get("last_scraped_at"))


def annotate_dead_streak(alias: dict, error_message: Optional[str], today: date) -> Optional[str]:
    """
    `error_message` with its "(dead since YYYY-MM-DD)" suffix when it is a
    dead-URL failure; any other message comes back unchanged. Report this
    string to the backend so the next attempt can read the streak start.
    """
    if classify_dead_reason(error_message) is None:
        return error_message
    base = DEAD_SINCE_RE.sub("", error_message)
    start = dead_streak_start(alias) or today
    return f"{base} (dead since {start.isoformat()})"


def should_mark_unavailable(alias: dict, error_message_now: Optional[str], today: date) -> Optional[str]:
    """
    Preset to mark `alias` unavailable with, or None to leave it alone.

    Requires: the current failure is dead-URL, and the alias has been failing
    with dead-URL reasons since at least MIN_DEAD_STREAK_DAYS before `today`.
    `alias` is the state from before the current attempt.
    """
    preset_now = classify_dead_reason(error_message_now)
    if preset_now is None:
        return None
    start = dead_streak_start(alias)
    if start is None or today - start < timedelta(days=MIN_DEAD_STREAK_DAYS):
        return None
    return preset_now
