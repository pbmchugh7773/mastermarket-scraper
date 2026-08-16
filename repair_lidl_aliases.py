#!/usr/bin/env python3
"""
Lidl broken-alias repair.

64 of Lidl's 77 aliases point at URLs that now return HTTP 404: Lidl rotates
the SKU segment of its product URLs while the product stays on sale.
discover_lidl_aliases.py cannot fix these — its candidate query excludes any
product that already has a Lidl alias, and these have one. Retiring them would
lose real coverage, since the products are still listed.

Two passes:
  * pass 1 `slug_exact`  — the old slug is still in the sitemap under a new SKU.
  * pass 2 `token_match` — token overlap + the HTML size gate, same engine and
    threshold discovery uses.

Proposal-only by default. `--apply` updates scraper_url for accepted repairs
and never touches unmatched aliases.

See docs/superpowers/specs/2026-08-10-lidl-alias-repair-design.md
"""
import re

import requests

from discover_lidl_aliases import USER_AGENT, HTTP_TIMEOUT

BROKEN_STATUSES = (404, 410)
REPAIR_THRESHOLD = 0.55

LIDL_PRODUCT_URL_RE = re.compile(r"/p/([^/?#]+)/p(\d+)")


def parse_lidl_url(url):
    """('potato-gratin', '10000681') for a Lidl PDP URL, else None."""
    if not url:
        return None
    m = LIDL_PRODUCT_URL_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2)


def select_broken_aliases(aliases):
    """
    Aliases worth attempting to repair.

    `last_scrape_success is False` is the breakage signal — None means the
    alias has never been scraped, which is not evidence of a dead URL. Aliases
    already flagged is_unavailable have been triaged by a human, and ones whose
    URL will not parse cannot be slug-matched.
    """
    out = []
    for alias in aliases:
        if alias.get("last_scrape_success") is not False:
            continue
        if alias.get("is_unavailable"):
            continue
        url = alias.get("scraper_url")
        if not url or parse_lidl_url(url) is None:
            continue
        out.append(alias)
    out.sort(key=lambda a: a["id"])
    return out


def classify_liveness(status):
    """
    'broken' | 'recovered' | 'inconclusive' for a liveness-check status.

    Only 404/410 justify replacing a URL. A 200 means the alias fixed itself
    and must be left alone. Anything else — 403, 500, a timeout surfacing as
    None — is inconclusive: repairing on it would swap a good URL for a guess.
    """
    if status in BROKEN_STATUSES:
        return "broken"
    if status == 200:
        return "recovered"
    return "inconclusive"


def check_url_liveness(url):
    """HTTP status for `url`, or None if the request could not complete."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code
    except requests.RequestException:
        return None
