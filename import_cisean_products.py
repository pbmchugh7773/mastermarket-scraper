#!/usr/bin/env python3
"""
Import new products from Cisean's open-source barcode database into MasterMarket.

Only imports products whose barcodes do NOT already exist in MasterMarket.

Usage:
    # Dry run — show what would be imported
    python import_cisean_products.py --dry-run

    # Import all new products
    python import_cisean_products.py

    # Import with limit
    python import_cisean_products.py --limit 50

    # Use a specific CSV file
    python import_cisean_products.py --csv /path/to/barcodes.csv

Environment Variables:
    API_URL              - MasterMarket API URL (default: https://api.mastermarketapp.com)
    SCRAPER_USERNAME     - MasterMarket account email
    SCRAPER_PASSWORD     - MasterMarket account password
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

# Configuration
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

OUTPUT_DIR = Path(__file__).parent / 'output' / 'imports'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            OUTPUT_DIR / f'import_cisean_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        )
    ]
)
logger = logging.getLogger(__name__)

# Category mapping: guess category from brand or product name
CATEGORY_HINTS = {
    # Brands → category
    'Avonmore': 'Fresh Food',
    'Kerrygold': 'Fresh Food',
    'Glenisk': 'Fresh Food',
    'Denny': 'Fresh Food',
    'Clonakilty': 'Fresh Food',
    'Brady Family': 'Fresh Food',
    'Coca-Cola': 'Drinks',
    'Pepsi': 'Drinks',
    'Lucozade': 'Drinks',
    'Club': 'Drinks',
    '7Up': 'Drinks',
    'Mi Wadi': 'Drinks',
    'Monster': 'Drinks',
    'Red Bull': 'Drinks',
    'Cadbury': 'Food Cupboard',
    'Tayto': 'Food Cupboard',
    'Pringles': 'Food Cupboard',
    "Jacob's": 'Food Cupboard',
    'Kellogg\'s': 'Food Cupboard',
    'Flahavan\'s': 'Food Cupboard',
    'Heinz': 'Food Cupboard',
    'Knorr': 'Food Cupboard',
    'Chef': 'Food Cupboard',
    'Batchelors': 'Food Cupboard',
    'Odlums': 'Food Cupboard',
    'Brennans': 'Fresh Food',
    'Pat The Baker': 'Fresh Food',
    'Fairy': 'Household',
    'Dettol': 'Household',
    'Domestos': 'Household',
    'Comfort': 'Household',
    'Pampers': 'Baby',
}

# Name keywords → category
NAME_CATEGORY_MAP = [
    (['milk', 'butter', 'cheese', 'yoghurt', 'yogurt', 'cream', 'egg'], 'Fresh Food'),
    (['bread', 'roll', 'wrap', 'bagel', 'croissant', 'scone'], 'Fresh Food'),
    (['chicken', 'beef', 'pork', 'bacon', 'sausage', 'ham', 'mince', 'lamb'], 'Fresh Food'),
    (['juice', 'water', 'cola', 'lemonade', 'beer', 'wine', 'vodka', 'gin', 'whiskey'], 'Drinks'),
    (['tea', 'coffee'], 'Drinks'),
    (['crisp', 'chip', 'chocolate', 'biscuit', 'sweet', 'candy', 'popcorn', 'nut'], 'Food Cupboard'),
    (['cereal', 'porridge', 'granola', 'muesli', 'oat'], 'Food Cupboard'),
    (['pasta', 'rice', 'noodle', 'spaghetti', 'penne'], 'Food Cupboard'),
    (['sauce', 'ketchup', 'mayo', 'oil', 'vinegar', 'mustard'], 'Food Cupboard'),
    (['soup', 'beans', 'tinned', 'canned', 'tuna'], 'Food Cupboard'),
    (['pizza', 'frozen', 'ice cream'], 'Frozen'),
    (['shampoo', 'toothpaste', 'deodorant', 'soap', 'shower'], 'Health & Beauty'),
    (['nappy', 'nappies', 'baby', 'formula', 'wipes'], 'Baby'),
    (['dog', 'cat', 'pet'], 'Pet'),
    (['washing', 'detergent', 'bleach', 'cleaner', 'bin bag'], 'Household'),
]


def guess_category(product_name: str, brand: str) -> str:
    """Guess product category from brand and name."""
    # Try brand first
    for brand_key, cat in CATEGORY_HINTS.items():
        if brand and brand_key.lower() in brand.lower():
            return cat

    # Try name keywords
    name_lower = product_name.lower()
    for keywords, cat in NAME_CATEGORY_MAP:
        if any(kw in name_lower for kw in keywords):
            return cat

    return 'General'


def parse_quantity_unit(product_name: str, package_size: str) -> tuple:
    """Extract quantity and unit from product name or package_size."""
    # Try package_size first (e.g., "1000", "330", "720g")
    if package_size:
        match = re.match(r'(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|cl|pk|pack)?', package_size, re.IGNORECASE)
        if match:
            qty = match.group(1)
            unit = match.group(2) or ''
            try:
                return int(float(qty)), unit.lower() if unit else None
            except ValueError:
                pass

    # Try product name (e.g., "Pepsi Max 330ml", "Butter 227g")
    match = re.search(r'(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|litre|cl|oz|pk|pack)\b', product_name, re.IGNORECASE)
    if match:
        try:
            return int(float(match.group(1))), match.group(2).lower()
        except ValueError:
            pass

    return None, None


class CiseanImporter:
    """Imports new products from Cisean barcode CSV into MasterMarket."""

    def __init__(self, dry_run: bool = False, limit: Optional[int] = None):
        self.dry_run = dry_run
        self.limit = limit
        self.session = requests.Session()
        self.token = None
        self.existing_barcodes: Set[str] = set()

        self.stats = {
            'cisean_total': 0,
            'already_exists': 0,
            'created': 0,
            'failed': 0,
            'skipped': 0,
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
                logger.info("Authenticated with MasterMarket API")
                return True
            logger.error(f"Auth failed: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

    def load_existing_barcodes(self):
        """Load existing barcodes from CSV cache or DB."""
        csv_path = Path(__file__).parent / 'mm_products_cache.csv'
        if csv_path.exists():
            logger.info(f"Loading existing barcodes from {csv_path}")
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    bc = (row.get('barcode') or '').strip()
                    if bc:
                        self.existing_barcodes.add(bc)
            logger.info(f"Loaded {len(self.existing_barcodes)} existing barcodes")
        else:
            logger.error("mm_products_cache.csv not found. Run discover_tesco_by_barcode.py first to generate it.")
            sys.exit(1)

    def create_product(self, barcode: str, name: str, brand: str,
                       category: str, quantity: Optional[int], unit: Optional[str]) -> bool:
        """Create a product via the MasterMarket API."""
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would create: {name} ({brand}) [{barcode}] → {category}")
            self.stats['created'] += 1
            return True

        payload = {
            'name': name,
            'description': name,  # Use name as description
            'category': category,
            'barcode': barcode,
            'brand': brand or None,
            'quantity': quantity,
            'unit': unit,
            'display_name': name,
        }

        try:
            resp = self.session.post(
                f"{API_URL}/products/",
                json=payload,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                product_id = resp.json().get('id', '?')
                logger.info(f"  Created [{product_id}]: {name} ({brand}) [{barcode}]")
                self.stats['created'] += 1
                self.existing_barcodes.add(barcode)
                return True
            else:
                logger.warning(f"  Failed: {resp.status_code} - {resp.text[:200]}")
                self.stats['failed'] += 1
                return False
        except Exception as e:
            logger.error(f"  Error: {e}")
            self.stats['failed'] += 1
            return False

    def run(self, csv_path: str):
        """Main import loop."""
        logger.info("=" * 60)
        logger.info("CISEAN BARCODE IMPORT FOR MASTERMARKET")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"CSV: {csv_path}")
        logger.info(f"Limit: {self.limit or 'none'}")
        logger.info("=" * 60)

        # Auth
        if not self.authenticate():
            return

        # Load existing
        self.load_existing_barcodes()

        # Read Cisean CSV
        cisean_products = []
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                cisean_products.append(row)

        self.stats['cisean_total'] = len(cisean_products)
        logger.info(f"Cisean products: {len(cisean_products)}")

        # Filter to new-only
        new_products = []
        for p in cisean_products:
            barcode = (p.get('barcode') or '').strip()
            if not barcode:
                self.stats['skipped'] += 1
                continue
            if barcode in self.existing_barcodes:
                self.stats['already_exists'] += 1
                continue
            new_products.append(p)

        logger.info(f"New products to import: {len(new_products)}")
        logger.info(f"Already exist: {self.stats['already_exists']}")

        if self.limit:
            new_products = new_products[:self.limit]
            logger.info(f"Limited to: {len(new_products)}")

        # Import
        for i, p in enumerate(new_products):
            barcode = p['barcode'].strip()
            name = (p.get('product_name') or '').strip()
            brand = (p.get('brand') or '').strip()
            package_size = (p.get('package_size') or '').strip()

            if not name:
                self.stats['skipped'] += 1
                continue

            category = guess_category(name, brand)
            quantity, unit = parse_quantity_unit(name, package_size)

            logger.info(f"[{i+1}/{len(new_products)}] {barcode} | {name} | {brand} → {category}")
            self.create_product(barcode, name, brand, category, quantity, unit)

            # Rate limit
            if not self.dry_run and (i + 1) % 20 == 0:
                time.sleep(1)

        # Save results
        output_file = OUTPUT_DIR / f'cisean_import_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_file, 'w') as f:
            json.dump({'timestamp': datetime.now().isoformat(), 'stats': self.stats}, f, indent=2)

        # Summary
        logger.info("\n" + "=" * 60)
        logger.info("IMPORT SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Cisean total:     {self.stats['cisean_total']}")
        logger.info(f"Already existed:  {self.stats['already_exists']}")
        logger.info(f"Created:          {self.stats['created']}")
        logger.info(f"Failed:           {self.stats['failed']}")
        logger.info(f"Skipped:          {self.stats['skipped']}")
        logger.info(f"Results: {output_file}")
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='Import Cisean barcode products into MasterMarket')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be imported')
    parser.add_argument('--limit', type=int, help='Max products to import')
    parser.add_argument('--csv', default='/tmp/cisean_barcodes.csv', help='Path to Cisean CSV')
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"ERROR: CSV not found at {args.csv}")
        sys.exit(1)

    importer = CiseanImporter(dry_run=args.dry_run, limit=args.limit)
    importer.run(args.csv)


if __name__ == '__main__':
    main()
