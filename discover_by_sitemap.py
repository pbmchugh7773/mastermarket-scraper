#!/usr/bin/env python3
"""
Sitemap-based Alias Discovery for MasterMarket

Discovers product aliases by fuzzy-matching product names from store sitemaps
against MasterMarket's product database. Works for any store with accessible
product URLs in their sitemap.

Supported stores: SuperValu, Dunnes Stores, Aldi

Strategy:
1. Parse product URLs from sitemap (XML file or URL)
2. Extract product name from URL slug
3. Fuzzy-match against MasterMarket products (by name)
4. Optionally verify by fetching the product page
5. Create aliases for high-confidence matches

Usage:
    # SuperValu discovery (fetch sitemap live)
    python discover_by_sitemap.py --store supervalu --dry-run

    # Dunnes discovery (use local sitemap file)
    python discover_by_sitemap.py --store dunnes --sitemap-file docs/temp/dunnes_sitemap.xml --dry-run

    # Limit matches and skip verification
    python discover_by_sitemap.py --store supervalu --limit 200 --no-verify --dry-run

    # Live run
    python discover_by_sitemap.py --store dunnes --sitemap-file docs/temp/dunnes_sitemap.xml
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
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

import requests

# Configuration
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

OUTPUT_DIR = Path(__file__).parent / 'output' / 'discovery'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'en-IE,en;q=0.9',
}

# Own-brand names by store — reject matches where product has store X own-brand
# but alias is for store Y (these are different products, not the same item)
OWN_BRAND_NAMES = {
    'Tesco': ['tesco'],
    'SuperValu': ['supervalu', 'daily basics'],
    'Dunnes Stores': ['dunnes stores', 'dunnes', 'simply better'],
    'Aldi': ['aldi', 'specially selected', 'nature\'s pick', 'cowbelle', 'brooklea',
             'bramwells', 'lacura', 'harvest morn', 'snackrite', 'moser roth',
             'cucina', 'lyttos', 'emporium', 'village bakery', 'the fishmonger'],
    'Lidl': ['lidl', 'cien', 'milbona', 'deluxe', 'silvercrest', 'parkside'],
}

# Store configurations
STORE_CONFIG = {
    'supervalu': {
        'display_name': 'SuperValu',
        'store_name': 'SuperValu',
        'sitemap_url': 'https://shop.supervalu.ie/sitemap.xml',
        'product_url_pattern': r'/product/',
        'slug_extract': lambda url: re.sub(r'-id-\d+$', '', url.split('/product/')[-1]) if '/product/' in url else '',
    },
    'dunnes': {
        'display_name': 'Dunnes Stores',
        'store_name': 'Dunnes Stores',
        'sitemap_url': None,  # Must use local file (Cloudflare)
        'product_url_pattern': r'/product/',
        'slug_extract': lambda url: re.sub(r'-id-\d+$', '', url.split('/product/')[-1]) if '/product/' in url else '',
    },
    'aldi': {
        'display_name': 'Aldi',
        'store_name': 'Aldi',
        'sitemap_url': 'https://www.aldi.ie/sitemap_products.xml',
        'product_url_pattern': r'/product/',
        'slug_extract': lambda url: re.sub(r'-0{5,}\d+$', '', url.split('/product/')[-1]) if '/product/' in url else '',
    },
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """Normalize a product name for comparison."""
    text = text.lower().strip()
    text = re.sub(r'\d+\s*(g|kg|ml|l|ltr|litre|cl|oz|pk|pack)\b', '', text)
    text = re.sub(r'\d+\s*x\s*\d+\s*(g|ml|kg)', '', text)
    text = re.sub(r'\b(the|a|an|of|in|with|and|&|for|from|style|original|classic)\b', '', text)
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_size_ml_or_g(text: str) -> Optional[float]:
    """Extract product size normalized to ml or g. Returns None if no size found.

    Handles multipacks: "4 pack 330ml" = 1320ml, "6 x 25g" = 150g
    Returns negative values for pack-only counts (no weight/volume).
    """
    text = text.lower()
    unit_mult = {'ml': 1, 'cl': 10, 'l': 1000, 'ltr': 1000, 'litre': 1000, 'g': 1, 'kg': 1000}

    # Multi-pack explicit: "4 x 330ml", "6x25g"
    m = re.search(r'(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(ml|g|l|kg|cl|ltr)', text)
    if m:
        count, val, unit = int(m.group(1)), float(m.group(2)), m.group(3)
        return count * val * unit_mult.get(unit, 1)

    # Multi-pack with "pack": "4 pack 330 ml", "6 pack 25g"
    m = re.search(r'(\d+)\s*(?:pack|pk)\s+(\d+(?:[.,]\d+)?)\s*(ml|g|l|kg|cl|ltr)', text)
    if m:
        count, val, unit = int(m.group(1)), float(m.group(2).replace(',', '.')), m.group(3)
        return count * val * unit_mult.get(unit, 1)

    # Single: "330ml", "1.5L", "500g", "1kg"
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|cl|l|ltr|litre|g|kg)\b', text)
    if m:
        val = float(m.group(1).replace(',', '.'))
        unit = m.group(2)
        return val * unit_mult.get(unit, 1)

    # Pack count only (no weight): "6 pack", "12pk"
    m = re.search(r'(\d+)\s*(pack|pk)\b', text)
    if m:
        return float(m.group(1)) * -1  # Negative = pack count

    return None


# Minimum word count for matching — reject very generic names
MIN_PRODUCT_WORDS = 2


def slug_to_words(slug: str) -> str:
    """Convert a URL slug to normalized words."""
    words = unquote(slug).replace('-', ' ').replace('&amp;', '').replace('%26', '')
    return normalize(words)


def word_overlap_score(query_words: set, candidate_words: set) -> float:
    """Calculate word overlap score."""
    if not query_words or not candidate_words:
        return 0.0
    intersection = query_words & candidate_words
    if not intersection:
        return 0.0
    query_coverage = len(intersection) / len(query_words)
    candidate_coverage = len(intersection) / len(candidate_words)
    return 0.7 * query_coverage + 0.3 * candidate_coverage


class SitemapDiscoverer:
    """Discovers aliases by fuzzy-matching sitemap URLs against MasterMarket products."""

    def __init__(self, store_key: str, dry_run: bool = False, min_score: float = 0.55):
        self.store_key = store_key
        self.config = STORE_CONFIG[store_key]
        self.dry_run = dry_run
        self.min_score = min_score
        self.session = requests.Session()
        self.token = None

        # Product index: normalized_name -> product info
        self.products: List[Dict] = []
        self.has_store: Set[int] = set()

        # Sitemap index
        self.sitemap_index: List[Dict] = []

        self.stats = {
            'sitemap_urls': 0,
            'products_loaded': 0,
            'existing_aliases': 0,
            'candidates': 0,
            'matches_found': 0,
            'aliases_created': 0,
            'aliases_failed': 0,
            'low_score': 0,
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
            logger.error(f"Auth failed: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

    def load_data(self):
        """Load products and aliases from CSV caches."""
        # Products
        csv_path = Path(__file__).parent / 'mm_products_cache.csv'
        if not csv_path.exists():
            logger.error("mm_products_cache.csv not found")
            sys.exit(1)

        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get('name') or '').strip()
                if not name:
                    continue
                norm = normalize(name)
                brand = (row.get('brand') or '').strip()
                brand_norm = normalize(brand) if brand else ''
                self.products.append({
                    'product_id': int(row['id']),
                    'name': name,
                    'brand': brand,
                    'norm': norm,
                    'words': set(norm.split()),
                    'brand_words': set(brand_norm.split()) if brand_norm else set(),
                })
                self.stats['products_loaded'] += 1

        # Aliases
        aliases_path = Path(__file__).parent / 'mm_aliases_cache.csv'
        if aliases_path.exists():
            with open(aliases_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('store_name') == self.config['store_name']:
                        self.has_store.add(int(row['product_id']))

        self.stats['existing_aliases'] = len(self.has_store)
        logger.info(f"Products: {self.stats['products_loaded']}, "
                     f"existing {self.config['display_name']} aliases: {len(self.has_store)}")

    def load_sitemap(self, sitemap_file: Optional[str] = None):
        """Load and parse sitemap."""
        xml_content = None

        if sitemap_file:
            logger.info(f"Loading sitemap from file: {sitemap_file}")
            with open(sitemap_file) as f:
                xml_content = f.read()
        elif self.config['sitemap_url']:
            logger.info(f"Fetching sitemap from: {self.config['sitemap_url']}")
            resp = requests.get(self.config['sitemap_url'], headers=HEADERS, timeout=30)
            resp.raise_for_status()
            xml_content = resp.text
        else:
            logger.error(f"No sitemap available for {self.config['display_name']}. Use --sitemap-file.")
            sys.exit(1)

        # Extract product URLs
        all_urls = re.findall(r'<loc>([^<]+)</loc>', xml_content)
        product_pattern = self.config['product_url_pattern']
        slug_extract = self.config['slug_extract']

        for url in all_urls:
            url_decoded = unquote(url).replace('&amp;', '&')
            if re.search(product_pattern, url_decoded):
                slug = slug_extract(url_decoded)
                if slug:
                    words = slug_to_words(slug)
                    word_set = set(words.split())
                    if len(word_set) >= 1:
                        self.sitemap_index.append({
                            'url': url_decoded,
                            'slug': slug,
                            'words': words,
                            'word_set': word_set,
                        })

        self.stats['sitemap_urls'] = len(self.sitemap_index)
        logger.info(f"Sitemap product URLs: {len(self.sitemap_index)}")

    def find_matches(self, limit: Optional[int] = None) -> List[Dict]:
        """Find best matches between products and sitemap URLs."""
        matches = []

        # Filter to products without this store alias
        candidates = [p for p in self.products if p['product_id'] not in self.has_store]
        self.stats['candidates'] = len(candidates)
        logger.info(f"Products without {self.config['display_name']} alias: {len(candidates)}")

        if limit:
            candidates = candidates[:limit]

        for i, product in enumerate(candidates):
            if (i + 1) % 500 == 0:
                logger.info(f"  Matching {i+1}/{len(candidates)}...")

            best_match = None
            best_score = 0

            for item in self.sitemap_index:
                score = word_overlap_score(product['words'], item['word_set'])

                # Brand bonus
                if product['brand_words'] and product['brand_words'] & item['word_set']:
                    score += 0.15

                if score > best_score:
                    best_score = score
                    best_match = item

            if best_match and best_score >= self.min_score:
                # Filter 1: Own-brand cross-match
                if self._is_own_brand_cross_match(product['name'], product['brand']):
                    self.stats['own_brand_rejected'] = self.stats.get('own_brand_rejected', 0) + 1
                    continue

                # Filter 2: Generic name (too few words = ambiguous match)
                if len(product['words']) < MIN_PRODUCT_WORDS:
                    self.stats['generic_name_rejected'] = self.stats.get('generic_name_rejected', 0) + 1
                    continue

                # Filter 3: Size mismatch — if both have size info, reject if >2x different
                if self._is_size_mismatch(product['name'], best_match['slug']):
                    self.stats['size_mismatch_rejected'] = self.stats.get('size_mismatch_rejected', 0) + 1
                    continue

                matches.append({
                    'product_id': product['product_id'],
                    'product_name': product['name'],
                    'brand': product['brand'],
                    'match_url': best_match['url'],
                    'match_slug': best_match['slug'],
                    'match_words': best_match['words'],
                    'score': round(best_score, 3),
                })
                self.stats['matches_found'] += 1
            else:
                self.stats['low_score'] += 1

        # Sort by score descending
        matches.sort(key=lambda x: x['score'], reverse=True)
        logger.info(f"Matches found: {len(matches)} (min score: {self.min_score})")
        return matches

    def _is_size_mismatch(self, product_name: str, match_slug: str) -> bool:
        """Check if product and match have significantly different sizes."""
        product_size = extract_size_ml_or_g(product_name)
        match_text = unquote(match_slug).replace('-', ' ')
        match_size = extract_size_ml_or_g(match_text)

        if product_size is None or match_size is None:
            return False  # Can't compare — allow the match

        # Both are pack counts (negative values)
        if product_size < 0 and match_size < 0:
            ratio = max(abs(product_size), abs(match_size)) / max(min(abs(product_size), abs(match_size)), 0.1)
            return ratio > 2.0

        # One is pack, other is weight — incomparable, allow
        if (product_size < 0) != (match_size < 0):
            return False

        # Both are weight/volume — compare
        if product_size > 0 and match_size > 0:
            ratio = max(product_size, match_size) / max(min(product_size, match_size), 0.1)
            return ratio > 2.0  # >2x size difference = likely different product

        return False

    def _is_own_brand_cross_match(self, product_name: str, product_brand: str) -> bool:
        """Check if product belongs to a different store's own-brand."""
        target_store = self.config['store_name']
        name_lower = product_name.lower()
        brand_lower = (product_brand or '').lower()

        for store, brands in OWN_BRAND_NAMES.items():
            if store == target_store:
                continue  # Skip own store — matching Tesco product to Tesco alias is fine
            for brand_name in brands:
                if brand_name in name_lower or brand_name in brand_lower:
                    return True
        return False

    def create_alias(self, product_id: int, alias_name: str, url: str) -> bool:
        if self.dry_run:
            self.stats['aliases_created'] += 1
            return True

        try:
            resp = self.session.post(
                f"{API_URL}/api/product-aliases/",
                json={
                    'product_id': product_id,
                    'store_name': self.config['store_name'],
                    'alias_name': alias_name,
                    'scraper_url': url,
                    'is_primary': False,
                    'is_active_for_scraping': True,
                    'country': 'IE',
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                self.stats['aliases_created'] += 1
                self.has_store.add(product_id)
                return True
            else:
                self.stats['aliases_failed'] += 1
                return False
        except Exception as e:
            logger.error(f"  Error: {e}")
            self.stats['aliases_failed'] += 1
            return False

    def run(self, sitemap_file: Optional[str] = None, limit: Optional[int] = None):
        store = self.config['display_name']
        logger.info("=" * 60)
        logger.info(f"{store.upper()} SITEMAP DISCOVERY FOR MASTERMARKET")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Min score: {self.min_score}, Limit: {limit or 'none'}")
        logger.info("=" * 60)

        if not self.authenticate():
            return

        self.load_data()
        self.load_sitemap(sitemap_file)

        if not self.sitemap_index:
            logger.error("No product URLs found in sitemap")
            return

        # Find matches
        matches = self.find_matches(limit)

        # Create aliases
        for m in matches:
            slug_name = m['match_slug'].replace('-', ' ').title()
            logger.info(f"  [{m['product_id']}] {m['product_name']} "
                         f"→ {slug_name} (score: {m['score']}) "
                         f"{'[DRY]' if self.dry_run else ''}")
            self.create_alias(m['product_id'], slug_name, m['match_url'])

        # Save results
        output_file = OUTPUT_DIR / f'{self.store_key}_discovery_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'store': store,
                'mode': 'dry_run' if self.dry_run else 'live',
                'stats': self.stats,
                'matches': matches[:50],  # Save top 50 for review
            }, f, indent=2, default=str)

        # Summary
        logger.info("\n" + "=" * 60)
        logger.info("DISCOVERY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Store:                    {store}")
        logger.info(f"Sitemap URLs:             {self.stats['sitemap_urls']}")
        logger.info(f"Products loaded:          {self.stats['products_loaded']}")
        logger.info(f"Existing aliases:         {self.stats['existing_aliases']}")
        logger.info(f"Candidates (no alias):    {self.stats['candidates']}")
        logger.info(f"Matches found:            {self.stats['matches_found']}")
        logger.info(f"Low score (skipped):      {self.stats['low_score']}")
        logger.info(f"Own-brand rejected:       {self.stats.get('own_brand_rejected', 0)}")
        logger.info(f"Generic name rejected:    {self.stats.get('generic_name_rejected', 0)}")
        logger.info(f"Size mismatch rejected:   {self.stats.get('size_mismatch_rejected', 0)}")
        logger.info(f"Aliases created:          {self.stats['aliases_created']}")
        logger.info(f"Aliases failed:           {self.stats['aliases_failed']}")
        logger.info(f"Results: {output_file}")
        logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='Discover aliases via store sitemap')
    parser.add_argument('--store', required=True, choices=STORE_CONFIG.keys(),
                        help='Store to discover')
    parser.add_argument('--sitemap-file', help='Path to local sitemap XML file')
    parser.add_argument('--dry-run', action='store_true', help='Show matches without creating')
    parser.add_argument('--limit', type=int, help='Max products to match')
    parser.add_argument('--min-score', type=float, default=0.55,
                        help='Minimum match score (default: 0.55)')
    args = parser.parse_args()

    discoverer = SitemapDiscoverer(
        store_key=args.store,
        dry_run=args.dry_run,
        min_score=args.min_score,
    )
    discoverer.run(sitemap_file=args.sitemap_file, limit=args.limit)


if __name__ == '__main__':
    main()
