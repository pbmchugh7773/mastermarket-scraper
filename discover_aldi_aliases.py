#!/usr/bin/env python3
"""
Aldi URL Discovery for MasterMarket

Finds Aldi product URLs for MasterMarket products that are missing Aldi prices.
Uses the Aldi.ie sitemap + fuzzy name matching + JSON-LD verification.

Strategy:
1. Fetch all Aldi product URLs from sitemap_products.xml
2. Query MasterMarket DB for products missing Aldi prices
3. Fuzzy-match product names to Aldi URLs (slug-based)
4. Verify top matches by fetching product page JSON-LD
5. Output: JSON ready to create aliases via API

Usage:
    # Dry run - show matches without creating aliases
    python discover_aldi_aliases.py --dry-run

    # Create aliases via API
    python discover_aldi_aliases.py

    # Limit to N products
    python discover_aldi_aliases.py --limit 10 --dry-run

    # Only products with 3+ existing stores (highest value)
    python discover_aldi_aliases.py --min-stores 3 --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f'discover_aldi_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    ]
)
logger = logging.getLogger(__name__)

# Configuration
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

ALDI_SITEMAP_URL = 'https://www.aldi.ie/sitemap_products.xml'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IE,en;q=0.9',
}


def normalize(text: str) -> str:
    """Normalize a product name for comparison."""
    text = text.lower().strip()
    # Remove common packaging suffixes
    text = re.sub(r'\d+\s*(g|kg|ml|l|ltr|litre|liter|cl|oz|pk|pack)\b', '', text)
    # Remove weight patterns like "4 x 28g" or "8x28g"
    text = re.sub(r'\d+\s*x\s*\d+\s*(g|ml|kg)', '', text)
    # Remove star ratings like "4.3 stars (145 Reviews)"
    text = re.sub(r'\d+\.?\d*\s*stars?\s*\(.*?\)', '', text)
    # Remove common filler words
    text = re.sub(r'\b(the|a|an|of|in|with|and|&|for|from|style|original|classic)\b', '', text)
    # Normalize whitespace and punctuation
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def slug_to_words(slug: str) -> str:
    """Convert a URL slug to normalized words."""
    # Extract the product part from URL: /product/{slug}-{barcode}
    # Remove the barcode (last segment after final hyphen group of digits)
    slug = re.sub(r'-0{5,}\d+$', '', slug)
    # Replace hyphens with spaces
    words = slug.replace('-', ' ')
    return normalize(words)


def word_overlap_score(query_words: set, candidate_words: set) -> float:
    """Calculate word overlap score between two sets of words."""
    if not query_words or not candidate_words:
        return 0.0
    intersection = query_words & candidate_words
    # Jaccard-like but weighted toward the query (we want all query words matched)
    if not intersection:
        return 0.0
    query_coverage = len(intersection) / len(query_words)
    candidate_coverage = len(intersection) / len(candidate_words)
    # Weighted: 70% query coverage (recall), 30% candidate coverage (precision)
    return 0.7 * query_coverage + 0.3 * candidate_coverage


def fetch_aldi_sitemap() -> List[str]:
    """Fetch all product URLs from Aldi.ie sitemap."""
    logger.info("Fetching Aldi sitemap...")
    try:
        resp = requests.get(ALDI_SITEMAP_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        urls = re.findall(r'<loc>(https://www\.aldi\.ie/product/[^<]+)</loc>', resp.text)
        logger.info(f"Found {len(urls)} Aldi product URLs in sitemap")
        return urls
    except Exception as e:
        logger.error(f"Failed to fetch sitemap: {e}")
        return []


def build_aldi_index(urls: List[str]) -> List[Dict]:
    """Build a searchable index from Aldi URLs."""
    index = []
    for url in urls:
        slug = url.split('/product/')[-1] if '/product/' in url else ''
        words = slug_to_words(slug)
        word_set = set(words.split())
        index.append({
            'url': url,
            'slug': slug,
            'words': words,
            'word_set': word_set,
        })
    return index


def find_best_matches(product_name: str, brand: str, aldi_index: List[Dict], top_n: int = 5) -> List[Dict]:
    """Find the best Aldi URL matches for a product."""
    # Build query from product name + brand
    query_norm = normalize(product_name)
    brand_norm = normalize(brand) if brand else ''

    # Also try without brand prefix (Aldi URLs often include brand)
    query_words = set(query_norm.split())
    brand_words = set(brand_norm.split()) if brand_norm else set()

    # Score all candidates
    scored = []
    for item in aldi_index:
        # Score with full query
        score = word_overlap_score(query_words, item['word_set'])

        # Bonus if brand matches in slug
        if brand_words and brand_words & item['word_set']:
            score += 0.15

        # Bonus for exact key-word sequences
        slug_lower = item['slug'].lower()
        name_key_parts = re.split(r'\s+', query_norm)
        consecutive_match = 0
        for part in name_key_parts:
            if part and len(part) > 2 and part in slug_lower:
                consecutive_match += 1
        if name_key_parts:
            score += 0.1 * (consecutive_match / len(name_key_parts))

        if score > 0.3:  # Minimum threshold
            scored.append({**item, 'score': round(score, 3)})

    # Sort by score descending
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored[:top_n]


def verify_product_page(url: str) -> Optional[Dict]:
    """Fetch a product page and extract JSON-LD data."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        # Extract JSON-LD Product data
        ld_matches = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            resp.text, re.DOTALL
        )
        for match in ld_matches:
            try:
                data = json.loads(match)
                if isinstance(data, dict) and data.get('@type') == 'Product':
                    return {
                        'name': data.get('name', ''),
                        'brand': data.get('brand', {}).get('name', ''),
                        'price': data.get('offers', {}).get('price', ''),
                        'currency': data.get('offers', {}).get('priceCurrency', ''),
                        'available': 'InStock' in str(data.get('offers', {}).get('availability', '')),
                        'image': data.get('image', [''])[0] if isinstance(data.get('image'), list) else data.get('image', ''),
                    }
            except json.JSONDecodeError:
                continue
        return None
    except Exception as e:
        logger.debug(f"Error fetching {url}: {e}")
        return None


class AldiDiscoverer:
    """Main discovery engine."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = requests.Session()
        self.token = None
        self.stats = {
            'products_checked': 0,
            'matches_found': 0,
            'aliases_created': 0,
            'verified': 0,
            'no_match': 0,
            'errors': 0,
        }

    def authenticate(self) -> bool:
        """Authenticate with MasterMarket API."""
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

    def get_products_missing_aldi(self, min_stores: int = 2, limit: int = 200) -> List[Dict]:
        """Get products that have prices in other stores but NOT Aldi.

        Uses two API calls:
        1. Get all aliases to know which products have Aldi
        2. Get product details for name/brand info
        """
        try:
            # Step 1: Get all aliases (paginated)
            logger.info("Fetching all aliases from API...")
            all_aliases = []
            page_limit = 1000
            offset = 0
            while True:
                resp = self.session.get(
                    f"{API_URL}/api/product-aliases/",
                    params={'limit': page_limit, 'offset': offset},
                    timeout=30,
                )
                if resp.status_code != 200:
                    logger.error(f"Failed to get aliases: {resp.status_code} - {resp.text[:200]}")
                    break
                data = resp.json()
                aliases = data.get('aliases', [])
                all_aliases.extend(aliases)
                if len(aliases) < page_limit:
                    break
                offset += page_limit

            logger.info(f"Retrieved {len(all_aliases)} aliases total")

            # Group by product_id
            product_stores = {}
            for alias in all_aliases:
                pid = alias['product_id']
                if pid not in product_stores:
                    product_stores[pid] = {
                        'product_id': pid,
                        'name': alias.get('alias_name', ''),
                        'stores': set(),
                        'has_aldi': False,
                    }
                product_stores[pid]['stores'].add(alias['store_name'])
                if alias['store_name'] == 'Aldi':
                    product_stores[pid]['has_aldi'] = True

            # Step 2: Get product details for name/brand from the products endpoint
            # For products missing Aldi, fetch their details
            candidate_pids = [
                pid for pid, info in product_stores.items()
                if not info['has_aldi'] and len(info['stores']) >= min_stores
            ]
            logger.info(f"Found {len(candidate_pids)} products missing Aldi with >={min_stores} stores")

            # Fetch product details in batches via the products endpoint
            missing = []
            for pid in candidate_pids:
                info = product_stores[pid]
                # Try to get product name/brand from the API
                try:
                    resp = self.session.get(
                        f"{API_URL}/products/{pid}",
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        prod = resp.json()
                        name = prod.get('name', info['name'])
                        brand = prod.get('brand', '')
                    else:
                        name = info['name']
                        brand = ''
                except Exception:
                    name = info['name']
                    brand = ''

                # Get reference price by searching product name
                ref_price = None
                try:
                    price_resp = self.session.get(
                        f"{API_URL}/products/all-with-prices",
                        params={'search': name[:60], 'limit': 1},
                        timeout=10,
                    )
                    if price_resp.status_code == 200:
                        price_data = price_resp.json()
                        products_list = price_data.get('products', [])
                        # Verify we got the right product by ID
                        if products_list and products_list[0].get('id') == pid:
                            recent = products_list[0].get('recent_prices', [])
                            prices_vals = [float(p['price']) for p in recent if p.get('price')]
                            if prices_vals:
                                ref_price = sum(prices_vals) / len(prices_vals)
                            elif products_list[0].get('lowest_price'):
                                ref_price = float(products_list[0]['lowest_price'])
                except Exception:
                    pass

                missing.append({
                    'product_id': pid,
                    'name': name,
                    'brand': brand,
                    'store_count': len(info['stores']),
                    'stores': sorted(info['stores']),
                    'ref_price': round(ref_price, 2) if ref_price else None,
                })

                if len(missing) >= limit:
                    break

            missing.sort(key=lambda x: x['store_count'], reverse=True)
            logger.info(f"Prepared {len(missing)} products for Aldi matching")
            return missing

        except Exception as e:
            logger.error(f"Error getting products: {e}")
            return []

    def create_alias(self, product_id: int, product_name: str, aldi_url: str, aldi_name: str) -> bool:
        """Create a product alias via the API."""
        if self.dry_run:
            logger.info(f"  [DRY RUN] Would create alias: product {product_id} -> {aldi_url}")
            return True

        try:
            resp = self.session.post(
                f"{API_URL}/api/product-aliases/",
                json={
                    'product_id': product_id,
                    'store_name': 'Aldi',
                    'alias_name': aldi_name,
                    'scraper_url': aldi_url,
                    'is_primary': False,
                    'is_active_for_scraping': True,
                    'country': 'IE',
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logger.info(f"  Created alias: product {product_id} -> {aldi_url}")
                return True
            else:
                logger.warning(f"  Failed to create alias: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"  Error creating alias: {e}")
            return False

    def run(self, min_stores: int = 2, limit: int = 200, verify: bool = True):
        """Main discovery loop."""
        logger.info("=" * 60)
        logger.info("ALDI URL DISCOVERY FOR MASTERMARKET")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Min stores: {min_stores}, Limit: {limit}, Verify: {verify}")
        logger.info("=" * 60)

        # Step 1: Auth
        if not self.dry_run and not self.authenticate():
            logger.error("Cannot authenticate. Aborting.")
            return

        if self.dry_run:
            self.authenticate()  # Still try, but don't fail

        # Step 2: Fetch Aldi sitemap
        aldi_urls = fetch_aldi_sitemap()
        if not aldi_urls:
            logger.error("No Aldi URLs found. Aborting.")
            return

        aldi_index = build_aldi_index(aldi_urls)

        # Step 3: Get products missing Aldi
        products = self.get_products_missing_aldi(min_stores=min_stores, limit=limit)
        if not products:
            logger.warning("No products found missing Aldi. Nothing to do.")
            return

        # Step 4: Match each product
        results = []
        for product in products:
            self.stats['products_checked'] += 1
            pid = product['product_id']
            name = product['name']
            brand = product['brand']

            ref_price = product.get('ref_price')
            ref_price_str = f", ref €{ref_price}" if ref_price else ""

            logger.info(f"\n[{self.stats['products_checked']}/{len(products)}] "
                        f"#{pid} {name} ({brand}) — stores: {', '.join(product['stores'])}{ref_price_str}")

            matches = find_best_matches(name, brand, aldi_index, top_n=3)

            if not matches:
                logger.info(f"  No matches found")
                self.stats['no_match'] += 1
                continue

            best = matches[0]
            logger.info(f"  Best match (score {best['score']}): {best['url']}")
            for m in matches[1:]:
                logger.info(f"  Alt match  (score {m['score']}): {m['url']}")

            # Verify the top match
            verified_data = None
            if verify and best['score'] >= 0.4:
                logger.info(f"  Verifying product page...")
                verified_data = verify_product_page(best['url'])
                if verified_data:
                    logger.info(f"  Verified: {verified_data['name']} "
                                f"({verified_data['brand']}) "
                                f"€{verified_data['price']} "
                                f"{'IN STOCK' if verified_data['available'] else 'OUT OF STOCK'}")
                    self.stats['verified'] += 1
                else:
                    logger.info(f"  Could not verify (page may be unavailable)")
                time.sleep(0.5)  # Rate limit

            # Price cross-validation
            price_ok = True
            price_note = ""
            if verified_data and ref_price and verified_data.get('price'):
                try:
                    aldi_price = float(verified_data['price'])
                    # Allow up to 60% deviation (grocery prices vary, promotions exist)
                    ratio = aldi_price / ref_price if ref_price > 0 else 999
                    if ratio < 0.25 or ratio > 4.0:
                        price_ok = False
                        price_note = f"PRICE MISMATCH: Aldi €{aldi_price} vs ref €{ref_price} (ratio {ratio:.1f}x)"
                        logger.warning(f"  {price_note}")
                        self.stats['price_rejected'] = self.stats.get('price_rejected', 0) + 1
                    elif ratio < 0.5 or ratio > 2.0:
                        price_note = f"PRICE WARNING: Aldi €{aldi_price} vs ref €{ref_price} (ratio {ratio:.1f}x) — may be different size"
                        logger.info(f"  {price_note}")
                    else:
                        price_note = f"Price OK: Aldi €{aldi_price} vs ref €{ref_price} (ratio {ratio:.1f}x)"
                        logger.info(f"  {price_note}")
                except (ValueError, TypeError):
                    pass

            # Brand cross-validation
            brand_ok = True
            brand_note = ""
            if verified_data and brand:
                aldi_brand = verified_data.get('brand', '').strip().upper()
                product_brand = brand.strip().upper()
                # Known Aldi own-brands that should NOT match branded products
                ALDI_OWN_BRANDS = {
                    'ACTILEAF', 'ALMAT', 'BRAMWELLS', 'BROOKLEA', 'COWBELLE',
                    'CUCINA', 'DOMINION', 'EMPORIUM', 'FOUR SEASONS',
                    'GIANNI\'S', 'HARVEST MORN', 'JUST TAPAS', 'KILKEELY',
                    'LACURA', 'LYTTOS', 'MAGNUM (ALDI)', 'MCGRATHS',
                    'MOSER ROTH', 'NATURE\'S PICK', 'NUTRIPOWER',
                    'POWER FORCE', 'SNACKRITE', 'SPECIALLY SELECTED',
                    'THE FISHMONGER', 'THE JUICE COMPANY', 'VILLAGE BAKERY',
                }
                if aldi_brand in ALDI_OWN_BRANDS:
                    brand_ok = False
                    brand_note = f"BRAND MISMATCH: product is {product_brand} but Aldi match is own-brand {aldi_brand}"
                    logger.warning(f"  {brand_note}")
                    self.stats['brand_rejected'] = self.stats.get('brand_rejected', 0) + 1
                elif product_brand and aldi_brand and product_brand != aldi_brand:
                    # Different brands but neither is Aldi own-brand - check if similar
                    # (e.g., "FAIRY" vs "FAIRY" is ok, "HEINZ" vs "HEINZ" is ok)
                    if product_brand in aldi_brand or aldi_brand in product_brand:
                        brand_note = f"Brand OK (partial): {product_brand} ~ {aldi_brand}"
                        logger.info(f"  {brand_note}")
                    else:
                        brand_ok = False
                        brand_note = f"BRAND MISMATCH: product is {product_brand} but Aldi is {aldi_brand}"
                        logger.warning(f"  {brand_note}")
                        self.stats['brand_rejected'] = self.stats.get('brand_rejected', 0) + 1
                else:
                    brand_note = f"Brand OK: {product_brand} = {aldi_brand}"
                    logger.info(f"  {brand_note}")

            result = {
                'product_id': pid,
                'product_name': name,
                'brand': brand,
                'existing_stores': product['stores'],
                'ref_price': ref_price,
                'match_score': best['score'],
                'aldi_url': best['url'],
                'aldi_slug': best['slug'],
                'verified': verified_data,
                'price_ok': price_ok,
                'price_note': price_note,
                'brand_ok': brand_ok,
                'brand_note': brand_note,
                'alt_matches': [{'url': m['url'], 'score': m['score']} for m in matches[1:]],
            }
            results.append(result)
            self.stats['matches_found'] += 1

            # Create alias if: high score + verified + price ok + brand ok
            if best['score'] >= 0.5 and (not verify or verified_data) and price_ok and brand_ok:
                aldi_name = verified_data['name'] if verified_data else best['words']
                if self.create_alias(pid, name, best['url'], aldi_name):
                    self.stats['aliases_created'] += 1

        # Step 5: Save results
        output_file = f'aldi_discovery_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'stats': self.stats,
                'results': results,
            }, f, indent=2)

        # Print summary
        logger.info("\n" + "=" * 60)
        logger.info("DISCOVERY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Products checked:  {self.stats['products_checked']}")
        logger.info(f"Matches found:     {self.stats['matches_found']}")
        logger.info(f"Verified:          {self.stats['verified']}")
        logger.info(f"Price rejected:    {self.stats.get('price_rejected', 0)}")
        logger.info(f"Brand rejected:    {self.stats.get('brand_rejected', 0)}")
        logger.info(f"Aliases created:   {self.stats['aliases_created']}")
        logger.info(f"No match:          {self.stats['no_match']}")
        logger.info(f"Results saved to:  {output_file}")

        # Print high-confidence matches for review
        high_conf = [r for r in results if r['match_score'] >= 0.5 and r.get('brand_ok', True) and r.get('price_ok', True)]
        brand_rejected = [r for r in results if not r.get('brand_ok', True)]
        low_conf = [r for r in results if 0.3 < r['match_score'] < 0.5 and r.get('brand_ok', True)]
        no_match_products = [p for p in products if p['product_id'] not in {r['product_id'] for r in results}]

        if high_conf:
            logger.info(f"\n HIGH CONFIDENCE MATCHES ({len(high_conf)}):")
            for r in high_conf:
                v = f" [VERIFIED: {r['verified']['name']} €{r['verified']['price']}]" if r['verified'] else ""
                price_flag = ""
                if not r.get('price_ok'):
                    price_flag = " ❌ PRICE MISMATCH"
                elif r.get('price_note') and 'WARNING' in r.get('price_note', ''):
                    price_flag = " ⚠️ PRICE WARNING"
                elif r.get('price_note') and 'OK' in r.get('price_note', ''):
                    price_flag = " ✅"
                ref = f" (ref €{r['ref_price']})" if r.get('ref_price') else ""
                logger.info(f"  #{r['product_id']} {r['product_name'][:50]} "
                            f"-> score={r['match_score']}{v}{ref}{price_flag}")
                logger.info(f"    URL: {r['aldi_url']}")

        if low_conf:
            logger.info(f"\n LOW CONFIDENCE (manual review needed) ({len(low_conf)}):")
            for r in low_conf:
                logger.info(f"  #{r['product_id']} {r['product_name'][:50]} "
                            f"-> score={r['match_score']}")
                logger.info(f"    URL: {r['aldi_url']}")

        if no_match_products:
            logger.info(f"\n NO MATCH ({len(no_match_products)}):")
            for p in no_match_products[:20]:
                logger.info(f"  #{p['product_id']} {p['name'][:60]} ({p['brand']})")


def main():
    parser = argparse.ArgumentParser(description='Discover Aldi URLs for MasterMarket products')
    parser.add_argument('--dry-run', action='store_true', help='Show matches without creating aliases')
    parser.add_argument('--limit', type=int, default=200, help='Max products to process')
    parser.add_argument('--min-stores', type=int, default=2, help='Min existing stores (default 2)')
    parser.add_argument('--no-verify', action='store_true', help='Skip page verification (faster)')
    args = parser.parse_args()

    discoverer = AldiDiscoverer(dry_run=args.dry_run)
    discoverer.run(
        min_stores=args.min_stores,
        limit=args.limit,
        verify=not args.no_verify,
    )


if __name__ == '__main__':
    main()
