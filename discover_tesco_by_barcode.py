#!/usr/bin/env python3
"""
Tesco Barcode Discovery for MasterMarket

Discovers new Tesco product aliases by matching EAN/GTIN barcodes from Apify
against MasterMarket's product database.

Strategy:
1. Fetch all MasterMarket products with barcodes (via API)
2. Crawl Tesco category pages via Apify to get product data + EAN
3. Match Apify results against MasterMarket barcodes
4. Create aliases for matched products that don't already have a Tesco alias

Usage:
    # Dry run — show matches without creating aliases
    python discover_tesco_by_barcode.py --dry-run

    # Run specific categories
    python discover_tesco_by_barcode.py --categories dairy drinks --dry-run

    # Run all categories
    python discover_tesco_by_barcode.py --all

    # Limit Apify results per category
    python discover_tesco_by_barcode.py --max-items 200 --dry-run

Environment Variables:
    APIFY_API_TOKEN      - Apify API token (required)
    API_URL              - MasterMarket API URL (default: https://api.mastermarketapp.com)
    SCRAPER_USERNAME     - MasterMarket scraper account email
    SCRAPER_PASSWORD     - MasterMarket scraper account password
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

try:
    from apify_client import ApifyClient
except ImportError:
    print("ERROR: apify-client not installed. Run: pip install apify-client")
    sys.exit(1)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

APIFY_TOKEN = os.getenv('APIFY_API_TOKEN')
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
SCRAPER_USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
SCRAPER_PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

ACTOR_ID = 'radeance/tesco-scraper'
REGION = 'IE'
STORE_NAME = 'Tesco'

OUTPUT_DIR = Path(__file__).parent / 'output' / 'discovery'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Discovery uses keyword search (NOT category URLs — the Apify actor doesn't support them).
# Each keyword triggers a Tesco search and the actor returns products with GTIN barcodes.
# Broad keywords return more results; specific keywords help fill gaps.
TESCO_KEYWORDS = {
    'dairy': ['milk', 'cheese', 'butter', 'yoghurt', 'cream', 'eggs'],
    'meat': ['chicken', 'beef', 'pork', 'sausages', 'bacon', 'mince', 'lamb'],
    'bakery': ['bread', 'rolls', 'wraps', 'croissant', 'bagel', 'scones'],
    'drinks': ['coca cola', 'pepsi', 'water', 'juice', 'tea', 'coffee', 'beer', 'wine'],
    'frozen': ['frozen pizza', 'frozen chips', 'ice cream', 'frozen vegetables', 'frozen fish'],
    'snacks': ['crisps', 'chocolate', 'biscuits', 'sweets', 'nuts', 'popcorn'],
    'cereals': ['cereal', 'porridge', 'granola', 'oats', 'muesli'],
    'cleaning': ['washing', 'detergent', 'bleach', 'kitchen roll', 'bin bags', 'soap'],
    'personal_care': ['shampoo', 'toothpaste', 'deodorant', 'shower gel', 'razor'],
    'baby': ['nappies', 'baby food', 'baby wipes', 'formula'],
    'pasta_rice': ['pasta', 'rice', 'noodles', 'couscous', 'spaghetti'],
    'tinned': ['tinned beans', 'tinned tomatoes', 'tinned soup', 'tinned tuna'],
    'sauces': ['ketchup', 'mayonnaise', 'cooking sauce', 'olive oil', 'vinegar', 'mustard'],
    'ready_meals': ['ready meal', 'pizza', 'sandwich', 'soup', 'salad'],
    'pet': ['dog food', 'cat food', 'pet treats'],
    'fruit_veg': ['apples', 'bananas', 'potatoes', 'onions', 'tomatoes', 'carrots'],
}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            OUTPUT_DIR / f'discover_tesco_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        )
    ]
)
logger = logging.getLogger(__name__)


class TescoBarcodeDiscoverer:
    """Discovers Tesco aliases by matching EAN barcodes against MasterMarket products."""

    def __init__(self, dry_run: bool = False, max_items: int = 500):
        self.dry_run = dry_run
        self.max_items = max_items
        self.session = requests.Session()
        self.token: Optional[str] = None
        self.apify_client = ApifyClient(APIFY_TOKEN) if APIFY_TOKEN else None

        # Barcode index: barcode -> product info
        self.barcode_index: Dict[str, Dict] = {}
        # Products that already have Tesco alias
        self.has_tesco: Set[int] = set()

        self.stats = {
            'mm_products_loaded': 0,
            'mm_with_barcode': 0,
            'existing_tesco_aliases': 0,
            'apify_products_fetched': 0,
            'apify_with_ean': 0,
            'barcode_matches': 0,
            'already_has_tesco': 0,
            'aliases_created': 0,
            'aliases_failed': 0,
            'categories_scraped': 0,
        }

    # ─────────────────────────────────────────
    # Authentication
    # ─────────────────────────────────────────

    def authenticate(self) -> bool:
        """Authenticate with MasterMarket API."""
        try:
            resp = self.session.post(
                f"{API_URL}/auth/login",
                data={"username": SCRAPER_USERNAME, "password": SCRAPER_PASSWORD},
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

    # ─────────────────────────────────────────
    # Load MasterMarket data
    # ─────────────────────────────────────────

    def load_products_and_aliases(self):
        """Load all MasterMarket products (with barcodes) and existing Tesco aliases.

        Uses two strategies:
        1. Direct DB query via psql (fast, preferred) for products
        2. API pagination for aliases (lighter endpoint)
        Falls back to API-only if DB is not accessible.
        """
        logger.info("Loading MasterMarket products and aliases...")

        # Step 1: Load aliases — prefer CSV cache, fallback to API
        aliases_csv = Path(__file__).parent / 'mm_aliases_cache.csv'
        if aliases_csv.exists():
            self._load_aliases_from_csv(aliases_csv)
        else:
            self._load_aliases_from_api()

        self.stats['existing_tesco_aliases'] = len(self.has_tesco)
        logger.info(f"Products with existing Tesco alias: {len(self.has_tesco)}")

        # Step 2: Load products with barcodes
        # Try CSV export first (faster than paginating API)
        csv_path = Path(__file__).parent / 'mm_products_cache.csv'
        if csv_path.exists():
            self._load_products_from_csv(csv_path)
        else:
            # Try direct DB export
            if self._export_products_from_db(csv_path):
                self._load_products_from_csv(csv_path)
            else:
                # Fallback: API pagination with retries
                self._load_products_from_api()

        logger.info(f"Loaded {self.stats['mm_products_loaded']} products, "
                     f"{self.stats['mm_with_barcode']} with valid barcodes")

    def _load_products_from_csv(self, csv_path: Path):
        """Load products from a CSV file."""
        import csv
        logger.info(f"Loading products from CSV: {csv_path}")
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                barcode = (row.get('barcode') or '').strip()
                if barcode and len(barcode) >= 8:
                    self.barcode_index[barcode] = {
                        'product_id': int(row['id']),
                        'name': row.get('name') or '',
                        'brand': row.get('brand') or '',
                        'category': row.get('category') or '',
                        'barcode': barcode,
                    }
                    self.stats['mm_with_barcode'] += 1
                self.stats['mm_products_loaded'] += 1

    def _export_products_from_db(self, csv_path: Path) -> bool:
        """Export products directly from production DB to CSV."""
        import subprocess
        logger.info("Exporting products from production DB...")
        try:
            env = os.environ.copy()
            env['PGPASSWORD'] = 'CZgnLJCPmcf92GuOpAKK'
            result = subprocess.run(
                [
                    'psql',
                    '-h', 'mastermarket-db.c5as4ek4st56.eu-west-1.rds.amazonaws.com',
                    '-U', 'mmarket_user',
                    '-d', 'mastermarket',
                    '-c', "COPY (SELECT id, name, barcode, brand, category FROM products "
                          "WHERE barcode IS NOT NULL AND barcode != '') TO STDOUT WITH CSV HEADER;",
                ],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if result.returncode == 0 and result.stdout.strip():
                with open(csv_path, 'w') as f:
                    f.write(result.stdout)
                logger.info(f"Exported products to {csv_path}")
                return True
            logger.warning(f"DB export failed: {result.stderr[:200]}")
            return False
        except Exception as e:
            logger.warning(f"DB export error: {e}")
            return False

    def _load_products_from_api(self):
        """Fallback: load products via API with longer timeouts."""
        logger.info("Loading products via API (this may be slow)...")
        offset = 0
        page_limit = 50  # Smaller pages to avoid timeouts
        retries = 0
        max_retries = 3
        while True:
            try:
                resp = self.session.get(
                    f"{API_URL}/products/all-with-prices",
                    params={'limit': page_limit, 'offset': offset},
                    timeout=60,
                )
                if resp.status_code != 200:
                    logger.error(f"Failed to get products: {resp.status_code}")
                    break
                data = resp.json()
                products = data.get('products', [])
                if not products:
                    break

                for p in products:
                    barcode = p.get('barcode', '')
                    if barcode and len(barcode) >= 8:
                        self.barcode_index[barcode] = {
                            'product_id': p['id'],
                            'name': p.get('name', ''),
                            'brand': p.get('brand', ''),
                            'category': p.get('category', ''),
                            'barcode': barcode,
                        }
                        self.stats['mm_with_barcode'] += 1

                self.stats['mm_products_loaded'] += len(products)
                offset += page_limit
                retries = 0

                if len(products) < page_limit:
                    break

            except Exception as e:
                retries += 1
                if retries > max_retries:
                    logger.error(f"Too many retries loading products. Stopping at offset {offset}")
                    break
                logger.warning(f"Retry {retries}/{max_retries} at offset {offset}: {e}")
                time.sleep(5)

    def _load_aliases_from_csv(self, csv_path: Path):
        """Load aliases from CSV cache."""
        import csv
        logger.info(f"Loading aliases from CSV: {csv_path}")
        count = 0
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('store_name') == 'Tesco':
                    self.has_tesco.add(int(row['product_id']))
                count += 1
        logger.info(f"Loaded {count} aliases from CSV")

    def _load_aliases_from_api(self):
        """Fallback: load aliases from API."""
        logger.info("Loading aliases from API...")
        all_aliases = []
        offset = 0
        page_limit = 1000
        while True:
            try:
                resp = self.session.get(
                    f"{API_URL}/api/product-aliases/",
                    params={'limit': page_limit, 'offset': offset},
                    timeout=60,
                )
                if resp.status_code != 200:
                    logger.error(f"Failed to get aliases: {resp.status_code}")
                    break
                data = resp.json()
                aliases = data.get('aliases', [])
                all_aliases.extend(aliases)
                if len(aliases) < page_limit:
                    break
                offset += page_limit
            except Exception as e:
                logger.error(f"Error loading aliases: {e}")
                break

        logger.info(f"Loaded {len(all_aliases)} aliases from API")
        for alias in all_aliases:
            if alias.get('store_name') == 'Tesco':
                self.has_tesco.add(alias['product_id'])

    # ─────────────────────────────────────────
    # Apify crawl
    # ─────────────────────────────────────────

    def crawl_keyword(self, keyword: str) -> List[Dict]:
        """Search Tesco via Apify keyword and return product items with GTIN."""
        if not self.apify_client:
            logger.error("Apify client not initialized (missing APIFY_API_TOKEN)")
            return []

        logger.info(f"  Searching Tesco for: '{keyword}' (max {self.max_items} items)")

        actor_input = {
            "keyword": keyword,
            "region": REGION,
            "max_items": self.max_items,
            "include_product_details": True,
            "only_unique": True,
        }

        try:
            run = self.apify_client.actor(ACTOR_ID).call(
                run_input=actor_input,
                timeout_secs=600,  # 10 minutes max per keyword
            )

            if not run:
                logger.error(f"Apify run failed for keyword '{keyword}'")
                return []

            status = run.get('status')
            if status not in ('SUCCEEDED', 'FINISHED'):
                logger.warning(f"Apify run status: {status}")

            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                logger.error("No dataset returned")
                return []

            items = list(self.apify_client.dataset(dataset_id).iterate_items())
            logger.info(f"  '{keyword}' returned {len(items)} items")

            return items

        except Exception as e:
            logger.error(f"Apify error for keyword '{keyword}': {e}")
            return []

    def crawl_category_keywords(self, category_key: str, keywords: List[str]) -> List[Dict]:
        """Crawl all keywords for a category, dedup by GTIN."""
        all_items = {}  # gtin -> item (dedup)

        for keyword in keywords:
            items = self.crawl_keyword(keyword)
            for item in items:
                gtin = item.get('gtin') or item.get('ean') or ''
                if gtin:
                    all_items[gtin] = item  # Dedup by barcode
            time.sleep(2)  # Rate limit between keywords

        unique_items = list(all_items.values())

        # Save combined output
        output_file = OUTPUT_DIR / f'tesco_{category_key}_{datetime.now().strftime("%Y%m%d")}.json'
        with open(output_file, 'w') as f:
            json.dump(unique_items, f, indent=2)
        logger.info(f"  Category total: {len(unique_items)} unique items (saved to {output_file.name})")

        return unique_items

    def load_cached_category_data(self, category_key: str) -> Optional[List[Dict]]:
        """Load previously cached Apify data if available from today."""
        today = datetime.now().strftime("%Y%m%d")
        cache_file = OUTPUT_DIR / f'tesco_{category_key}_{today}.json'
        if cache_file.exists():
            logger.info(f"  Found cached data: {cache_file.name}")
            with open(cache_file) as f:
                return json.load(f)
        return None

    # ─────────────────────────────────────────
    # Barcode matching
    # ─────────────────────────────────────────

    def match_apify_items(self, items: List[Dict]) -> List[Dict]:
        """Match Apify items against MasterMarket barcodes."""
        matches = []

        for item in items:
            self.stats['apify_products_fetched'] += 1

            # Extract EAN/GTIN from Apify result
            ean = item.get('ean') or item.get('gtin') or item.get('upc') or ''
            ean = str(ean).strip()

            if not ean or len(ean) < 8:
                continue

            self.stats['apify_with_ean'] += 1

            # Normalize: some EANs have leading zeros stripped
            # Standard EAN-13 is 13 digits, EAN-8 is 8 digits
            ean_variants = [ean]
            if len(ean) < 13:
                ean_variants.append(ean.zfill(13))  # Pad to 13 digits
            if len(ean) == 14:
                ean_variants.append(ean[1:])  # Strip leading 0 from GTIN-14

            # Try matching
            mm_product = None
            matched_ean = None
            for variant in ean_variants:
                if variant in self.barcode_index:
                    mm_product = self.barcode_index[variant]
                    matched_ean = variant
                    break

            if not mm_product:
                continue

            self.stats['barcode_matches'] += 1
            pid = mm_product['product_id']

            # Check if product already has Tesco alias
            if pid in self.has_tesco:
                self.stats['already_has_tesco'] += 1
                continue

            # Extract Tesco URL and product info
            tesco_url = item.get('url', '')
            # Build URL from product_id if not present
            if not tesco_url and item.get('product_id'):
                tesco_url = f"https://www.tesco.ie/groceries/en-IE/products/{item['product_id']}"
            tesco_name = item.get('title', '') or item.get('name', '')
            tesco_price = None
            try:
                tesco_price = float(item.get('price') or item.get('currentPrice') or 0)
            except (TypeError, ValueError):
                pass

            matches.append({
                'product_id': pid,
                'mm_name': mm_product['name'],
                'mm_brand': mm_product['brand'],
                'mm_barcode': mm_product['barcode'],
                'tesco_name': tesco_name,
                'tesco_url': tesco_url,
                'tesco_price': tesco_price,
                'matched_ean': matched_ean,
            })

        return matches

    # ─────────────────────────────────────────
    # Create aliases
    # ─────────────────────────────────────────

    def create_alias(self, product_id: int, tesco_name: str, tesco_url: str) -> bool:
        """Create a product alias via the MasterMarket API."""
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would create alias: product {product_id} -> {tesco_url}")
            self.stats['aliases_created'] += 1
            return True

        try:
            resp = self.session.post(
                f"{API_URL}/api/product-aliases/",
                json={
                    'product_id': product_id,
                    'store_name': STORE_NAME,
                    'alias_name': tesco_name,
                    'scraper_url': tesco_url,
                    'is_primary': False,
                    'is_active_for_scraping': True,
                    'country': 'IE',
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"  Created alias: product {product_id} -> {tesco_url}")
                self.stats['aliases_created'] += 1
                self.has_tesco.add(product_id)  # Prevent duplicates within same run
                return True
            else:
                logger.warning(f"  Failed: {resp.status_code} - {resp.text[:200]}")
                self.stats['aliases_failed'] += 1
                return False
        except Exception as e:
            logger.error(f"  Error creating alias: {e}")
            self.stats['aliases_failed'] += 1
            return False

    # ─────────────────────────────────────────
    # Main run
    # ─────────────────────────────────────────

    def run(self, categories: List[str], use_cache: bool = False):
        """Main discovery loop."""
        logger.info("=" * 60)
        logger.info("TESCO BARCODE DISCOVERY FOR MASTERMARKET")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Categories: {', '.join(categories)}")
        logger.info(f"Max items per category: {self.max_items}")
        logger.info(f"Use cache: {use_cache}")
        logger.info("=" * 60)

        # Step 1: Authenticate
        if not self.authenticate():
            logger.error("Cannot authenticate. Aborting.")
            return

        # Step 2: Load products and aliases
        self.load_products_and_aliases()

        if not self.barcode_index:
            logger.error("No products with barcodes found. Aborting.")
            return

        candidates_available = len(self.barcode_index) - len(self.has_tesco)
        logger.info(f"\nProducts without Tesco alias: {candidates_available}")
        logger.info(f"(Total with barcode: {len(self.barcode_index)}, "
                     f"already have Tesco: {len(self.has_tesco)})")

        # Step 3: Crawl each category (via keyword search) and match
        all_matches = []
        for cat_key in categories:
            if cat_key not in TESCO_KEYWORDS:
                logger.warning(f"Unknown category: {cat_key}. Skipping.")
                continue

            keywords = TESCO_KEYWORDS[cat_key]
            logger.info(f"\n{'─' * 50}")
            logger.info(f"Category: {cat_key} (keywords: {', '.join(keywords)})")
            logger.info(f"{'─' * 50}")

            # Try cache first
            items = None
            if use_cache:
                items = self.load_cached_category_data(cat_key)

            # Crawl if no cache
            if items is None:
                items = self.crawl_category_keywords(cat_key, keywords)

            if not items:
                logger.warning(f"No items returned for {cat_name}")
                continue

            self.stats['categories_scraped'] += 1

            # Match barcodes
            matches = self.match_apify_items(items)
            logger.info(f"  Barcode matches: {len(matches)} new aliases to create")

            # Create aliases
            for match in matches:
                logger.info(f"\n  MATCH: [{match['product_id']}] "
                            f"{match['mm_name']} (barcode: {match['matched_ean']})")
                logger.info(f"    → Tesco: {match['tesco_name']}")
                logger.info(f"    → URL: {match['tesco_url']}")
                if match['tesco_price']:
                    logger.info(f"    → Price: €{match['tesco_price']:.2f}")

                self.create_alias(
                    match['product_id'],
                    match['tesco_name'],
                    match['tesco_url'],
                )

            all_matches.extend(matches)

            # Rate limit between categories
            if cat_key != categories[-1]:
                logger.info("  Waiting 5s before next category...")
                time.sleep(5)

        # Step 4: Save results
        output_file = OUTPUT_DIR / f'tesco_discovery_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'mode': 'dry_run' if self.dry_run else 'live',
                'stats': self.stats,
                'matches': all_matches,
            }, f, indent=2, default=str)

        # Step 5: Print summary
        logger.info("\n" + "=" * 60)
        logger.info("DISCOVERY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"MasterMarket products loaded:   {self.stats['mm_products_loaded']}")
        logger.info(f"  with valid barcodes:          {self.stats['mm_with_barcode']}")
        logger.info(f"  existing Tesco aliases:       {self.stats['existing_tesco_aliases']}")
        logger.info(f"Categories scraped:             {self.stats['categories_scraped']}")
        logger.info(f"Apify products fetched:         {self.stats['apify_products_fetched']}")
        logger.info(f"  with EAN/GTIN:                {self.stats['apify_with_ean']}")
        logger.info(f"Barcode matches:                {self.stats['barcode_matches']}")
        logger.info(f"  already had Tesco:            {self.stats['already_has_tesco']}")
        logger.info(f"Aliases created:                {self.stats['aliases_created']}")
        logger.info(f"Aliases failed:                 {self.stats['aliases_failed']}")
        logger.info(f"\nResults saved to: {output_file}")
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Discover Tesco product aliases by matching EAN barcodes'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Show matches without creating aliases')
    parser.add_argument('--categories', nargs='+',
                        help=f'Categories to crawl. Available: {", ".join(TESCO_KEYWORDS.keys())}')
    parser.add_argument('--all', action='store_true',
                        help='Crawl all categories')
    parser.add_argument('--max-items', type=int, default=500,
                        help='Max products per Apify category crawl (default: 500)')
    parser.add_argument('--use-cache', action='store_true',
                        help="Use cached Apify data from today if available")
    parser.add_argument('--list-categories', action='store_true',
                        help='List available categories and exit')

    args = parser.parse_args()

    if args.list_categories:
        print("Available Tesco categories:")
        for key, keywords in TESCO_KEYWORDS.items():
            print(f"  {key:20s} keywords: {', '.join(keywords)}")
        sys.exit(0)

    if not APIFY_TOKEN and not args.use_cache:
        print("ERROR: APIFY_API_TOKEN environment variable not set")
        print("Set it with: export APIFY_API_TOKEN='your-token-here'")
        sys.exit(1)

    # Determine categories
    if args.all:
        categories = list(TESCO_KEYWORDS.keys())
    elif args.categories:
        categories = args.categories
    else:
        # Default: highest-value categories first
        categories = ['dairy', 'drinks', 'snacks', 'cereals', 'meat', 'frozen']
        print(f"No categories specified. Using defaults: {', '.join(categories)}")
        print(f"Use --all for all categories or --categories <list> to specify.")

    discoverer = TescoBarcodeDiscoverer(
        dry_run=args.dry_run,
        max_items=args.max_items,
    )
    discoverer.run(categories, use_cache=args.use_cache)


if __name__ == '__main__':
    main()
