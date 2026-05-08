#!/usr/bin/env python3
"""Audit existing product_aliases for silent redirects (READ-ONLY).

Reuses the same _validate_pdp_redirect helper that simple_local_to_prod.py uses
defensively at scrape-time. Iterates every is_active_for_scraping=True alias,
issues a single GET, and records every alias whose final URL doesn't match its
store's PDP convention.

Output: a CSV the admin reviews manually before flipping is_unavailable=True
on each suspect alias via the admin UI (or a future bulk endpoint).

Usage:
    export API_URL=https://api.mastermarketapp.com
    export SCRAPER_USERNAME=admin@mastermarket.com
    export SCRAPER_PASSWORD=...
    python audit_redirect_aliases.py [--limit 100] [--store Tesco] [--out path.csv]

This script does NOT mutate any state. It does NOT POST anywhere except for
authentication. Safe to run against production.
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import requests

from simple_local_to_prod import _validate_pdp_redirect, PDP_PATTERNS

API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

DEFAULT_TIMEOUT = 15
DEFAULT_RATE_LIMIT_S = 1.0  # gentle on supermarket sites; raise if rate-limited

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def authenticate(session: requests.Session) -> str:
    """Return JWT token. Reuses the SCRAPER_USERNAME/SCRAPER_PASSWORD env vars
    that the existing scraper already requires; no new credentials needed."""
    resp = session.post(
        f'{API_URL}/auth/login',
        data={'username': USERNAME, 'password': PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def fetch_active_aliases(session: requests.Session, store: Optional[str], limit: int):
    """Pull active aliases via the existing /api/product-aliases endpoint."""
    params = {'skip': 0, 'limit': min(limit, 1000), 'is_active_for_scraping': True}
    if store:
        params['store_name'] = store
    resp = session.get(f'{API_URL}/api/product-aliases', params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get('items', [])


def probe_alias(alias: dict, http_timeout: int) -> dict:
    """GET the alias URL and run the PDP validator. Returns a dict suitable for
    CSV output. Network errors are recorded as separate rows so they're visible."""
    scraper_url = alias.get('scraper_url')
    store = alias.get('store_name', '')
    record = {
        'alias_id': alias.get('id'),
        'product_id': alias.get('product_id'),
        'store_name': store,
        'alias_name': alias.get('alias_name'),
        'scraper_url': scraper_url,
        'final_url': '',
        'status_code': '',
        'error': '',
        'last_scraped_at': alias.get('last_scraped_at') or '',
    }
    if not scraper_url:
        record['error'] = 'no scraper_url on alias'
        return record
    try:
        resp = requests.get(
            scraper_url,
            timeout=http_timeout,
            allow_redirects=True,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; MasterMarket-Audit/1.0)',
                'Accept-Language': 'en-IE,en;q=0.9',
            },
        )
        record['final_url'] = resp.url
        record['status_code'] = resp.status_code
        ok, err = _validate_pdp_redirect(store, scraper_url, resp.url)
        if not ok:
            record['error'] = err
    except requests.RequestException as exc:
        record['error'] = f'network error: {exc.__class__.__name__}: {exc}'
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=1000, help='Max aliases to inspect')
    parser.add_argument('--store', default=None, help='Restrict to one store_name')
    parser.add_argument('--rate', type=float, default=DEFAULT_RATE_LIMIT_S,
                        help='Seconds between requests')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument('--out', default=None, help='Output CSV path')
    args = parser.parse_args()

    if args.store and args.store.lower() not in PDP_PATTERNS:
        logger.warning(
            f"Store '{args.store}' has no PDP pattern registered — every redirect "
            f"will be treated as suspicious. Known stores: {sorted(PDP_PATTERNS)}"
        )

    out_path = args.out or f'audit_redirect_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'

    session = requests.Session()
    logger.info(f"Authenticating against {API_URL} as {USERNAME}")
    token = authenticate(session)
    session.headers['Authorization'] = f'Bearer {token}'

    logger.info(
        f"Fetching active aliases (store={args.store or 'ALL'}, limit={args.limit})"
    )
    aliases = fetch_active_aliases(session, args.store, args.limit)
    logger.info(f"Got {len(aliases)} aliases to probe")

    suspect_rows = []
    error_rows = []
    fields = [
        'alias_id', 'product_id', 'store_name', 'alias_name',
        'scraper_url', 'final_url', 'status_code', 'error', 'last_scraped_at',
    ]
    with open(out_path, 'w', newline='', encoding='utf-8') as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()

        for i, alias in enumerate(aliases, 1):
            record = probe_alias(alias, args.timeout)
            if record['error']:
                if record['error'].startswith('network error'):
                    error_rows.append(record)
                else:
                    suspect_rows.append(record)
                writer.writerow(record)
                fp.flush()
                logger.warning(
                    f"[{i}/{len(aliases)}] {record['store_name']} alias {record['alias_id']} "
                    f"({record.get('alias_name','')}): {record['error']}"
                )
            else:
                logger.info(
                    f"[{i}/{len(aliases)}] {record['store_name']} alias {record['alias_id']} OK"
                )
            time.sleep(args.rate)

    logger.info(f"\n=== AUDIT SUMMARY ===")
    logger.info(f"Total aliases probed: {len(aliases)}")
    logger.info(f"Suspect (redirected): {len(suspect_rows)}")
    logger.info(f"Network errors:      {len(error_rows)}")
    logger.info(f"Output CSV:          {out_path}")
    if suspect_rows:
        per_store = {}
        for r in suspect_rows:
            per_store[r['store_name']] = per_store.get(r['store_name'], 0) + 1
        logger.info("Suspect by store:")
        for store, n in sorted(per_store.items()):
            logger.info(f"  {store}: {n}")
    return 0 if not suspect_rows else 1  # nonzero exit if anything found


if __name__ == '__main__':
    sys.exit(main())
