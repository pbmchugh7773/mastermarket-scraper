#!/usr/bin/env python3
"""
Import unmatched Tesco products from cached Apify data into MasterMarket.

Reads the cached Apify JSON files from output/discovery/, finds products
whose barcodes don't exist in MasterMarket, and creates them with a
Tesco alias ready for scraping.

Usage:
    python import_tesco_unmatched.py --dry-run
    python import_tesco_unmatched.py --limit 200
    python import_tesco_unmatched.py
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

OUTPUT_DIR = Path(__file__).parent / 'output' / 'imports'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DISCOVERY_DIR = Path(__file__).parent / 'output' / 'discovery'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            OUTPUT_DIR / f'import_tesco_unmatched_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        )
    ]
)
logger = logging.getLogger(__name__)

# Category guessing from Tesco's own category/department data
TESCO_DEPT_MAP = {
    'fresh food': 'Fresh Food',
    'bakery': 'Fresh Food',
    'dairy': 'Fresh Food',
    'meat': 'Fresh Food',
    'fruit': 'Fresh Food',
    'vegetable': 'Fresh Food',
    'drink': 'Drinks',
    'beverage': 'Drinks',
    'tea': 'Drinks',
    'coffee': 'Drinks',
    'alcohol': 'Drinks',
    'wine': 'Drinks',
    'beer': 'Drinks',
    'frozen': 'Frozen',
    'ice cream': 'Frozen',
    'household': 'Household',
    'cleaning': 'Household',
    'laundry': 'Household',
    'baby': 'Baby',
    'pet': 'Pet',
    'health': 'Health & Beauty',
    'beauty': 'Health & Beauty',
    'toiletries': 'Health & Beauty',
}

NAME_CATEGORY_MAP = [
    (['milk', 'butter', 'cheese', 'yoghurt', 'yogurt', 'cream', 'egg'], 'Fresh Food'),
    (['bread', 'roll', 'wrap', 'bagel', 'croissant'], 'Fresh Food'),
    (['chicken', 'beef', 'pork', 'bacon', 'sausage', 'ham', 'mince', 'lamb', 'turkey'], 'Fresh Food'),
    (['juice', 'water', 'cola', 'lemonade', 'squash', 'cordial'], 'Drinks'),
    (['tea', 'coffee', 'beer', 'wine', 'vodka', 'gin', 'whiskey', 'lager', 'cider'], 'Drinks'),
    (['crisp', 'chip', 'chocolate', 'biscuit', 'sweet', 'candy', 'popcorn', 'nut'], 'Food Cupboard'),
    (['cereal', 'porridge', 'granola', 'muesli', 'oat'], 'Food Cupboard'),
    (['pasta', 'rice', 'noodle', 'spaghetti', 'sauce', 'ketchup', 'mayo', 'oil'], 'Food Cupboard'),
    (['soup', 'beans', 'tinned', 'canned', 'tuna'], 'Food Cupboard'),
    (['pizza', 'frozen', 'ice cream'], 'Frozen'),
    (['shampoo', 'toothpaste', 'deodorant', 'soap', 'shower'], 'Health & Beauty'),
    (['nappy', 'nappies', 'baby', 'formula'], 'Baby'),
    (['dog', 'cat', 'pet'], 'Pet'),
    (['washing', 'detergent', 'bleach', 'cleaner'], 'Household'),
]


def guess_category(name: str, department: str = '') -> str:
    dept_lower = department.lower()
    for key, cat in TESCO_DEPT_MAP.items():
        if key in dept_lower:
            return cat
    name_lower = name.lower()
    for keywords, cat in NAME_CATEGORY_MAP:
        if any(kw in name_lower for kw in keywords):
            return cat
    return 'Food Cupboard'


class TescoUnmatchedImporter:
    def __init__(self, dry_run: bool = False, limit: Optional[int] = None):
        self.dry_run = dry_run
        self.limit = limit
        self.session = requests.Session()
        self.token = None
        self.existing_barcodes: Set[str] = set()
        self.stats = {
            'apify_items_loaded': 0,
            'with_gtin': 0,
            'already_exists': 0,
            'created': 0,
            'alias_created': 0,
            'failed': 0,
            'skipped_own_brand': 0,
        }

    def authenticate(self) -> bool:
        try:
            resp = self.session.post(
                f"{API_URL}/auth/login",
                data={"username": USERNAME, "password": PASSWORD},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if resp.status_code == 200:
                self.token = resp.json().get('access_token')
                self.session.headers['Authorization'] = f'Bearer {self.token}'
                logger.info("Authenticated")
                return True
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

    def load_existing_barcodes(self):
        csv_path = Path(__file__).parent / 'mm_products_cache.csv'
        if not csv_path.exists():
            logger.error("mm_products_cache.csv not found")
            sys.exit(1)
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                bc = (row.get('barcode') or '').strip()
                if bc:
                    self.existing_barcodes.add(bc)
        logger.info(f"Existing barcodes: {len(self.existing_barcodes)}")

    def load_apify_items(self) -> List[Dict]:
        """Load all cached Apify Tesco JSON files."""
        all_items = {}
        for f in sorted(DISCOVERY_DIR.glob('tesco_*.json')):
            try:
                with open(f) as fh:
                    items = json.load(fh)
                for item in items:
                    gtin = item.get('gtin') or item.get('ean') or ''
                    gtin = str(gtin).strip()
                    if gtin and len(gtin) >= 8:
                        all_items[gtin] = item
                logger.info(f"  Loaded {len(items)} items from {f.name}")
            except Exception as e:
                logger.warning(f"  Error loading {f.name}: {e}")

        self.stats['apify_items_loaded'] = len(all_items)
        logger.info(f"Total unique Apify items: {len(all_items)}")
        return list(all_items.values())

    def create_product_with_alias(self, item: Dict) -> bool:
        gtin = str(item.get('gtin') or item.get('ean') or '').strip()
        name = (item.get('name') or item.get('title') or '').strip()
        brand = (item.get('brand_name') or '').strip()
        department = (item.get('department') or item.get('category') or '').strip()
        url = item.get('url', '')
        product_id_tesco = item.get('product_id', '')

        if not url and product_id_tesco:
            url = f"https://www.tesco.ie/groceries/en-IE/products/{product_id_tesco}"

        if not name:
            self.stats['failed'] += 1
            return False

        category = guess_category(name, department)

        # Extract quantity/unit from name
        quantity, unit = None, None
        m = re.search(r'(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|cl|pk|pack)\b', name, re.IGNORECASE)
        if m:
            try:
                quantity = int(float(m.group(1)))
                unit = m.group(2).lower()
            except ValueError:
                pass

        if self.dry_run:
            logger.info(f"  [DRY] Create: {name} ({brand}) [{gtin}] → {category}")
            self.stats['created'] += 1
            self.stats['alias_created'] += 1
            return True

        # Create product
        try:
            resp = self.session.post(
                f"{API_URL}/products/",
                json={
                    'name': name,
                    'description': name,
                    'category': category,
                    'barcode': gtin,
                    'brand': brand or None,
                    'quantity': quantity,
                    'unit': unit,
                    'display_name': name,
                },
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                logger.warning(f"  Product failed: {resp.status_code} - {resp.text[:100]}")
                self.stats['failed'] += 1
                return False

            new_product_id = resp.json().get('id')
            self.stats['created'] += 1
            self.existing_barcodes.add(gtin)

            # Create Tesco alias
            alias_resp = self.session.post(
                f"{API_URL}/api/product-aliases/",
                json={
                    'product_id': new_product_id,
                    'store_name': 'Tesco',
                    'alias_name': name,
                    'scraper_url': url,
                    'is_primary': True,
                    'is_active_for_scraping': True,
                    'country': 'IE',
                },
                timeout=15,
            )
            if alias_resp.status_code in (200, 201):
                self.stats['alias_created'] += 1
                logger.info(f"  [{new_product_id}] {name} + Tesco alias")
            else:
                logger.warning(f"  Product created but alias failed: {alias_resp.status_code}")

            return True
        except Exception as e:
            logger.error(f"  Error: {e}")
            self.stats['failed'] += 1
            return False

    def run(self):
        logger.info("=" * 60)
        logger.info("IMPORT UNMATCHED TESCO PRODUCTS")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Limit: {self.limit or 'none'}")
        logger.info("=" * 60)

        if not self.authenticate():
            return

        self.load_existing_barcodes()
        items = self.load_apify_items()

        # Filter to unmatched only
        new_items = []
        for item in items:
            gtin = str(item.get('gtin') or item.get('ean') or '').strip()
            if not gtin or len(gtin) < 8:
                continue
            self.stats['with_gtin'] += 1

            # Normalize barcode variants
            variants = [gtin]
            if len(gtin) < 13:
                variants.append(gtin.zfill(13))
            if len(gtin) == 14:
                variants.append(gtin[1:])

            if any(v in self.existing_barcodes for v in variants):
                self.stats['already_exists'] += 1
                continue

            # Skip Tesco own-brand? No — Tesco own-brand products ARE valid
            # since they're real products sold in Tesco
            new_items.append(item)

        logger.info(f"Unmatched items with GTIN: {len(new_items)}")
        logger.info(f"Already in DB: {self.stats['already_exists']}")

        if self.limit:
            new_items = new_items[:self.limit]
            logger.info(f"Limited to: {len(new_items)}")

        for i, item in enumerate(new_items):
            if (i + 1) % 100 == 0:
                logger.info(f"  Progress: {i+1}/{len(new_items)}")
            self.create_product_with_alias(item)
            if not self.dry_run and (i + 1) % 20 == 0:
                time.sleep(1)

        # Summary
        logger.info("\n" + "=" * 60)
        logger.info("IMPORT SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Apify items loaded:     {self.stats['apify_items_loaded']}")
        logger.info(f"With GTIN:              {self.stats['with_gtin']}")
        logger.info(f"Already in DB:          {self.stats['already_exists']}")
        logger.info(f"Products created:       {self.stats['created']}")
        logger.info(f"Tesco aliases created:  {self.stats['alias_created']}")
        logger.info(f"Failed:                 {self.stats['failed']}")
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='Import unmatched Tesco products from Apify cache')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()

    importer = TescoUnmatchedImporter(dry_run=args.dry_run, limit=args.limit)
    importer.run()


if __name__ == '__main__':
    main()
