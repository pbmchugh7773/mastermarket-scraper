#!/usr/bin/env python3
"""
Apify-based Tesco Scraper for MasterMarket.

Uses Apify's Tesco UK & Ireland Scraper actor to fetch prices
and upload them to MasterMarket API.

Schedule: Martes y Viernes (via GitHub Actions)

Usage:
    # Production (default)
    python apify_tesco_scraper.py

    # With custom limit
    python apify_tesco_scraper.py --limit 50

    # Dry run (no uploads)
    python apify_tesco_scraper.py --dry-run
"""
import os
import sys
import argparse
import time
import json
import re
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

try:
    from apify_client import ApifyClient
except ImportError:
    print("ERROR: apify-client not installed. Run: pip install apify-client")
    sys.exit(1)


# Configuration from environment variables
APIFY_TOKEN = os.getenv('APIFY_API_TOKEN')
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
SCRAPER_USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
SCRAPER_PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

# Apify Actor configuration
ACTOR_ID = 'radeance/tesco-scraper'
REGION = 'IE'  # Ireland (for Apify actor "region" parameter)
STORE_NAME = 'Tesco'
STORE_LOCATION = 'IE'
CURRENCY = 'EUR'

# Output directory for saving Apify JSON responses
OUTPUT_DIR = Path(__file__).parent / 'output' / 'apify'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class ApifyTescoScraper:
    """Scraper that uses Apify's Tesco actor to fetch prices."""

    def __init__(self, dry_run: bool = False, limit: int = None, retry_mode: bool = False):
        """
        Initialize the scraper.

        Args:
            dry_run: If True, don't upload prices to MasterMarket
            limit: Maximum number of products to scrape (None = all)
            retry_mode: If True, only scrape pending/failed aliases
        """
        if not APIFY_TOKEN:
            raise ValueError("APIFY_API_TOKEN environment variable not set")

        self.apify_client = ApifyClient(APIFY_TOKEN)
        self.session = requests.Session()
        self.token: Optional[str] = None
        self.dry_run = dry_run
        self.limit = limit
        self.retry_mode = retry_mode

        # Statistics
        self.stats = {
            'total_aliases': 0,
            'urls_sent_to_apify': 0,
            'results_from_apify': 0,
            'prices_uploaded': 0,
            'prices_failed': 0,
            'prices_skipped': 0,
            'matched_by_url': 0,
            'matched_by_product_id': 0
        }

    # Apify's URL validation pattern for Tesco scraper
    TESCO_URL_PATTERN = re.compile(
        r'^https://www\.tesco\.(?:com|ie)/groceries/en-(?:GB|IE)/(?:shop|products|search)'
    )

    @staticmethod
    def extract_tesco_product_id(url: str) -> Optional[str]:
        """
        Extract Tesco product ID from URL.

        Handles both .ie and .com domains:
        - https://www.tesco.ie/groceries/en-IE/products/308088804
        - https://www.tesco.com/groceries/en-GB/products/308088804

        Returns:
            Product ID as string, or None if not found
        """
        if not url:
            return None

        # Pattern matches /products/DIGITS at the end of URL
        match = re.search(r'/products/(\d+)', url)
        if match:
            return match.group(1)
        return None

    @classmethod
    def is_valid_tesco_url(cls, url: str) -> bool:
        """
        Validate if URL matches Apify's expected Tesco URL pattern.

        Args:
            url: URL to validate

        Returns:
            True if URL is valid for Apify Tesco scraper
        """
        if not url:
            return False
        return bool(cls.TESCO_URL_PATTERN.match(url))

    def authenticate_mastermarket(self) -> bool:
        """Authenticate with MasterMarket API."""
        try:
            response = self.session.post(
                f"{API_URL}/auth/login",
                data={
                    "username": SCRAPER_USERNAME,
                    "password": SCRAPER_PASSWORD
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            self.token = data.get('access_token')

            if self.token:
                self.session.headers['Authorization'] = f'Bearer {self.token}'
                return True
            return False

        except requests.RequestException as e:
            print(f"Authentication failed: {e}")
            return False

    def get_tesco_aliases(self) -> List[Dict]:
        """Get all Tesco product aliases with scraper URLs."""
        try:
            params = {"store_name": STORE_NAME, "limit": self.limit or 500}
            response = self.session.get(
                f"{API_URL}/api/product-aliases/",
                params=params,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            # Handle different response formats
            if isinstance(data, list):
                aliases = data
            elif isinstance(data, dict):
                aliases = data.get('aliases', [])
            else:
                aliases = []

            self.stats['total_aliases'] = len(aliases)
            return aliases

        except requests.RequestException as e:
            print(f"Failed to fetch aliases: {e}")
            return []

    def get_pending_aliases(self) -> List[Dict]:
        """Get pending aliases that need scraping (retry mode, same as simple_local_to_prod.py)."""
        try:
            params = {
                'store_name': STORE_NAME,
                'limit': self.limit or 500,
                'retry_mode': True
            }

            response = self.session.get(
                f'{API_URL}/api/scraping/pending-aliases',
                params=params,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            # Handle different response formats
            if isinstance(data, list):
                aliases = data
            elif isinstance(data, dict):
                aliases = data.get('aliases', [])
            else:
                aliases = []

            self.stats['total_aliases'] = len(aliases)
            print(f"  Found {len(aliases)} pending aliases to retry")
            return aliases

        except requests.RequestException as e:
            print(f"Failed to fetch pending aliases: {e}")
            return []

    def run_apify_scraper(self, urls: List[str]) -> List[Dict]:
        """
        Run Apify Tesco scraper actor and return results.

        Args:
            urls: List of Tesco product URLs to scrape

        Returns:
            List of scraped product data
        """
        if not urls:
            print("No URLs to scrape")
            return []

        # Filter out invalid URLs before sending to Apify
        valid_urls = []
        invalid_urls = []
        for url in urls:
            if self.is_valid_tesco_url(url):
                valid_urls.append(url)
            else:
                invalid_urls.append(url)

        if invalid_urls:
            print(f"  ⚠️ Filtered out {len(invalid_urls)} invalid URL(s):")
            for url in invalid_urls[:5]:  # Show first 5
                print(f"     - {url[:80]}...")
            if len(invalid_urls) > 5:
                print(f"     ... and {len(invalid_urls) - 5} more")
            self.stats['urls_filtered'] = len(invalid_urls)

        if not valid_urls:
            print("ERROR: No valid URLs to scrape after filtering")
            return []

        urls = valid_urls
        self.stats['urls_sent_to_apify'] = len(urls)

        # Prepare actor input - using correct schema for radeance/tesco-scraper
        # Key parameters:
        # - urls: Array of strings (NOT startUrls with objects)
        # - region: "IE" for Ireland (NOT country)
        # - max_items: Maximum products to retrieve
        # - include_product_details: Get full product info including EAN/GTIN
        actor_input = {
            "urls": urls,  # Simple string array, not objects
            "region": REGION,  # Ireland
            "max_items": len(urls) + 50,  # Buffer for potential duplicates
            "include_product_details": True,
            "only_unique": True
        }

        print(f"  Region: {REGION}")
        print(f"  URLs sample: {urls[:2]}..." if len(urls) > 2 else f"  URLs: {urls}")

        print(f"Starting Apify actor '{ACTOR_ID}' with {len(urls)} URLs...")
        print("This may take 5-15 minutes depending on the number of products...")

        try:
            # Start actor and wait for completion
            run = self.apify_client.actor(ACTOR_ID).call(
                run_input=actor_input,
                timeout_secs=1800  # 30 minutes max
            )

            if not run:
                print("ERROR: Apify actor run failed (no response)")
                return []

            # Check run status
            status = run.get('status')
            if status not in ('SUCCEEDED', 'FINISHED'):
                print(f"WARNING: Actor run status: {status}")

            # Get results from dataset
            dataset_id = run.get('defaultDatasetId')
            if not dataset_id:
                print("ERROR: No dataset ID returned from actor run")
                return []

            dataset_items = self.apify_client.dataset(dataset_id).list_items().items
            self.stats['results_from_apify'] = len(dataset_items)

            print(f"Got {len(dataset_items)} results from Apify")

            # Save complete JSON response for later review/reprocessing
            self.save_apify_response(dataset_items, run)

            return dataset_items

        except Exception as e:
            print(f"ERROR running Apify actor: {e}")
            return []

    def save_apify_response(self, items: List[Dict], run_info: Dict) -> Optional[Path]:
        """
        Save the complete Apify response to a JSON file.

        Args:
            items: List of product data from Apify
            run_info: Apify run metadata

        Returns:
            Path to saved file or None if failed
        """
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        filename = f"apify_tesco_{timestamp}.json"
        filepath = OUTPUT_DIR / filename

        try:
            output_data = {
                'timestamp': timestamp,
                'actor_id': ACTOR_ID,
                'run_id': run_info.get('id'),
                'dataset_id': run_info.get('defaultDatasetId'),
                'status': run_info.get('status'),
                'total_items': len(items),
                'items': items
            }

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)

            print(f"  Saved Apify response to: {filepath}")
            self.stats['json_saved'] = str(filepath)
            return filepath

        except Exception as e:
            print(f"  WARNING: Failed to save JSON: {e}")
            return None

    def extract_price_data(self, item: Dict) -> Optional[Dict]:
        """
        Extract price and promotion data from Apify result.

        IMPORTANT: Following MasterMarket price architecture:
        - membership_price: price = Clubcard price, original_price = regular price
        - multi_buy: price = regular unit price, promotion_text = deal description

        Args:
            item: Single product result from Apify

        Returns:
            Dict with price data or None if invalid
        """
        # Try different field names for regular price
        regular_price = (
            item.get('price') or
            item.get('currentPrice') or
            item.get('regularPrice')
        )

        if not regular_price:
            return None

        # Ensure price is a float
        try:
            regular_price = float(regular_price)
        except (TypeError, ValueError):
            return None

        # Validate price range (€0.01 - €1000)
        if regular_price < 0.01 or regular_price > 1000:
            return None

        result = {
            'price': regular_price,  # Default to regular price
            'original_price': regular_price,  # Same by default
            'url': item.get('url', ''),
            'title': item.get('title', ''),
            'ean': item.get('ean') or item.get('gtin') or item.get('upc'),
        }

        # Check for promotions in the promotion object
        promotion = item.get('promotion')
        if promotion and isinstance(promotion, dict):
            terms = promotion.get('terms', '')
            description = promotion.get('description', '')

            # First, check if it's a MULTI_BUY promotion
            # Patterns: "Any 3 for €5", "2 for €6", "3 for 2", etc.
            multi_buy_match = re.search(
                r'(?:any\s+)?(\d+)\s+for\s+[€£]?\s*(\d+(?:[.,]\d{2})?)',
                description,
                re.IGNORECASE
            )

            if multi_buy_match:
                # This is a MULTI_BUY promotion
                # Keep price as regular unit price, store the deal in promotion_text
                result['promotion_type'] = 'multi_buy'
                result['promotion_text'] = description  # Full description like "Any 3 for €5 Clubcard Price"
                # Don't change price - multi_buy uses regular unit price
                # The basket calculation will apply the deal based on quantity
                return result

            # If not multi_buy, check for simple Clubcard price
            if 'CLUBCARD' in terms.upper() or 'clubcard' in description.lower():
                # Extract price from description like "€8.00 Save 1/3 Clubcard Price"
                # IMPORTANT: Require currency symbol to avoid matching "1/3" from "Save 1/3"
                # Patterns supported:
                #   - "€8.00 ..." or "£8.00 ..." (currency + price)
                #   - "90p Clubcard" (pence format)
                price_match = re.search(
                    r'[€£]\s*(\d+(?:[.,]\d{2})?)',  # Currency symbol required
                    description
                )
                # Fallback: Check for pence format "XXp" (no currency symbol)
                if not price_match:
                    pence_match = re.search(r'^(\d+)p\s', description)
                    if pence_match:
                        # Convert pence to euros/pounds
                        price_match = pence_match
                if price_match:
                    price_str = price_match.group(1).replace(',', '.')
                    try:
                        clubcard_price = float(price_str)
                        # Handle pence (e.g., "90p" = 0.90)
                        if 'p ' in description.lower() or description.lower().endswith('p'):
                            if clubcard_price > 10:  # Likely pence not pounds
                                clubcard_price = clubcard_price / 100

                        # Validate Clubcard price
                        if 0.01 <= clubcard_price < regular_price:
                            # ARCHITECTURE: price = promotional (Clubcard), original_price = regular
                            result['price'] = clubcard_price  # What customer pays
                            result['original_price'] = regular_price  # Price without membership
                            result['promotion_type'] = 'membership_price'
                            result['promotion_text'] = 'Clubcard Price'
                            result['promotion_discount_value'] = regular_price - clubcard_price
                    except ValueError:
                        pass

        # Legacy: Check direct fields for Clubcard price
        clubcard_price = item.get('clubcardPrice') or item.get('promoPrice')
        if clubcard_price and 'promotion_type' not in result:
            try:
                clubcard_price = float(clubcard_price)
                if 0.01 <= clubcard_price < regular_price:
                    result['price'] = clubcard_price
                    result['original_price'] = regular_price
                    result['promotion_type'] = 'membership_price'
                    result['promotion_text'] = 'Clubcard Price'
                    result['promotion_discount_value'] = regular_price - clubcard_price
            except (TypeError, ValueError):
                pass

        return result

    def upload_price(self, product_id: int, price_data: Dict) -> bool:
        """
        Upload a price to MasterMarket API.

        Args:
            product_id: MasterMarket product ID
            price_data: Dict with price and optional promotion data

        Returns:
            True if upload succeeded
        """
        if self.dry_run:
            print(f"  [DRY RUN] Would upload: product_id={product_id}, price={price_data['price']}, original_price={price_data.get('original_price')}, promotion_type={price_data.get('promotion_type')}")
            return True

        payload = {
            "product_id": product_id,
            "store_name": STORE_NAME,
            "store_location": STORE_LOCATION,
            "price": price_data['price'],
            "currency": CURRENCY,
            "country": STORE_LOCATION
        }

        # Add promotion data if available
        # Architecture: price = promotional, original_price = regular
        if 'promotion_type' in price_data:
            payload['promotion_type'] = price_data['promotion_type']
        if 'promotion_text' in price_data:
            payload['promotion_text'] = price_data['promotion_text']
        if 'original_price' in price_data:
            payload['original_price'] = price_data['original_price']
        if 'promotion_discount_value' in price_data:
            payload['promotion_discount_value'] = price_data['promotion_discount_value']

        # Retry logic
        for attempt in range(3):
            try:
                response = self.session.post(
                    f"{API_URL}/api/community-prices/submit-scraped",
                    json=payload,
                    timeout=30
                )

                if response.status_code in (200, 201):
                    return True
                elif response.status_code == 429:
                    # Rate limited - back off
                    time.sleep(4)
                    continue
                else:
                    print(f"  Upload failed: {response.status_code} - {response.text[:100]}")
                    return False

            except requests.RequestException as e:
                print(f"  Upload error: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)  # Exponential backoff

        return False

    def update_scraping_status(self, alias_id: int, success: bool, price: float = None,
                              error_message: str = None, promotion_type: str = None,
                              promotion_text: str = None, original_price: float = None) -> bool:
        """Update scraping status for an alias (same as simple_local_to_prod.py)"""
        if self.dry_run:
            print(f"  [DRY RUN] Would update status: alias_id={alias_id}, success={success}")
            return True

        try:
            data = {
                'alias_id': alias_id,
                'success': success,
                'price': price,
                'error_message': error_message,
                'promotion_type': promotion_type,
                'promotion_text': promotion_text,
                'original_price': original_price
            }

            response = self.session.post(
                f'{API_URL}/api/scraping/update-status',
                json=data,
                timeout=30
            )

            if response.status_code == 200:
                return True
            else:
                print(f"  WARNING: Failed to update status for alias {alias_id}: {response.status_code}")
                return False

        except Exception as e:
            print(f"  ERROR: Failed to update status for alias {alias_id}: {e}")
            return False

    def run(self) -> Dict[str, Any]:
        """
        Main execution flow.

        Returns:
            Statistics dictionary
        """
        start_time = datetime.now()
        print(f"\n{'='*60}")
        print(f"Apify Tesco Scraper - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Mode: {'DRY RUN' if self.dry_run else 'PRODUCTION'}")
        if self.retry_mode:
            print(f"Retry Mode: ENABLED (only pending/failed aliases)")
        print(f"API: {API_URL}")
        print(f"{'='*60}\n")

        # Step 1: Authenticate with MasterMarket
        print("[1/4] Authenticating with MasterMarket...")
        if not self.authenticate_mastermarket():
            print("ERROR: Failed to authenticate with MasterMarket")
            return self.stats
        print("  Authenticated successfully")

        # Step 2: Get Tesco aliases (all or pending based on retry_mode)
        if self.retry_mode:
            print("\n[2/4] Fetching pending Tesco aliases (retry mode)...")
            aliases = self.get_pending_aliases()
            if not aliases:
                print("  No pending aliases found - all products up to date!")
                return self.stats
        else:
            print("\n[2/4] Fetching Tesco product aliases...")
            aliases = self.get_tesco_aliases()

        # Build mappings for matching:
        # 1. url_to_product: Direct URL match
        # 2. tesco_id_to_product: Match by Tesco product ID (handles .ie vs .com)
        # 3. url_to_alias_id: For updating scraping status
        url_to_product = {}
        tesco_id_to_product = {}
        url_to_alias_id = {}

        for alias in aliases:
            scraper_url = alias.get('scraper_url')
            product_id = alias.get('product_id')
            alias_id = alias.get('id')  # ProductAlias.id
            if scraper_url and product_id:
                url_to_product[scraper_url] = product_id
                if alias_id:
                    url_to_alias_id[scraper_url] = alias_id

                # Also map by Tesco product ID for cross-domain matching
                tesco_id = self.extract_tesco_product_id(scraper_url)
                if tesco_id:
                    tesco_id_to_product[tesco_id] = product_id

        urls = list(url_to_product.keys())
        print(f"  Found {len(aliases)} aliases, {len(urls)} with scraper URLs")
        print(f"  Built {len(tesco_id_to_product)} Tesco product ID mappings")

        if not urls:
            print("ERROR: No URLs to scrape")
            return self.stats

        # Apply limit if specified
        if self.limit and len(urls) > self.limit:
            urls = urls[:self.limit]
            print(f"  Limited to {self.limit} URLs")

        # Step 3: Run Apify scraper
        print(f"\n[3/4] Running Apify scraper...")
        results = self.run_apify_scraper(urls)

        if not results:
            print("ERROR: No results from Apify")
            return self.stats

        # Step 4: Upload prices to MasterMarket
        print(f"\n[4/4] Uploading prices to MasterMarket...")

        for i, item in enumerate(results):
            url = item.get('url', '')
            apify_product_id = item.get('product_id') or item.get('sku')

            # Strategy 1: Direct URL match
            product_id = url_to_product.get(url)
            if product_id:
                self.stats['matched_by_url'] += 1

            # Strategy 2: Match by Tesco product ID (handles .ie vs .com domain differences)
            if not product_id:
                tesco_id = self.extract_tesco_product_id(url) or apify_product_id
                if tesco_id and tesco_id in tesco_id_to_product:
                    product_id = tesco_id_to_product[tesco_id]
                    self.stats['matched_by_product_id'] += 1

            # Strategy 3: Partial URL match (fallback)
            if not product_id:
                for stored_url, pid in url_to_product.items():
                    if url in stored_url or stored_url in url:
                        product_id = pid
                        break

            if not product_id:
                self.stats['prices_skipped'] += 1
                continue

            # Get alias_id for this URL (needed to update scraping status)
            alias_id = url_to_alias_id.get(url)

            # Extract price data
            price_data = self.extract_price_data(item)
            if not price_data:
                self.stats['prices_skipped'] += 1
                # Update status as failed (no price found)
                if alias_id:
                    self.update_scraping_status(
                        alias_id=alias_id,
                        success=False,
                        error_message="No valid price found in Apify data"
                    )
                continue

            # Upload price
            success = self.upload_price(product_id, price_data)

            if success:
                self.stats['prices_uploaded'] += 1
                # Update scraping status as successful
                if alias_id:
                    self.update_scraping_status(
                        alias_id=alias_id,
                        success=True,
                        price=price_data['price'],
                        promotion_type=price_data.get('promotion_type'),
                        promotion_text=price_data.get('promotion_text'),
                        original_price=price_data.get('original_price')
                    )
            else:
                self.stats['prices_failed'] += 1
                # Update status as failed (upload failed)
                if alias_id:
                    self.update_scraping_status(
                        alias_id=alias_id,
                        success=False,
                        error_message="Upload to API failed"
                    )

            # Progress update
            if (i + 1) % 50 == 0:
                print(f"  Progress: {i + 1}/{len(results)} processed...")

        # Print summary
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"  Total aliases:        {self.stats['total_aliases']}")
        print(f"  URLs sent to Apify:   {self.stats['urls_sent_to_apify']}")
        print(f"  Results from Apify:   {self.stats['results_from_apify']}")
        print(f"  Matched by URL:       {self.stats['matched_by_url']}")
        print(f"  Matched by product ID:{self.stats['matched_by_product_id']}")
        print(f"  Prices uploaded:      {self.stats['prices_uploaded']}")
        print(f"  Prices failed:        {self.stats['prices_failed']}")
        print(f"  Prices skipped:       {self.stats['prices_skipped']}")
        if self.stats.get('urls_filtered'):
            print(f"  URLs filtered (invalid): {self.stats['urls_filtered']}")
        print(f"  Elapsed time:         {elapsed:.1f} seconds")
        if self.stats.get('json_saved'):
            print(f"  JSON saved to:        {self.stats['json_saved']}")
        print(f"{'='*60}\n")

        return self.stats


def main():
    parser = argparse.ArgumentParser(
        description="Apify-based Tesco Scraper for MasterMarket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run without uploading prices to MasterMarket'
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Maximum number of products to scrape'
    )

    parser.add_argument(
        '--retry-mode',
        action='store_true',
        help='Only scrape pending/failed aliases (for retry runs)'
    )

    args = parser.parse_args()

    # Validate API token
    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN environment variable not set")
        print("Please set it with your Apify API token from:")
        print("https://console.apify.com/settings/integrations")
        sys.exit(1)

    # Run scraper
    try:
        scraper = ApifyTescoScraper(
            dry_run=args.dry_run,
            limit=args.limit,
            retry_mode=args.retry_mode
        )
        stats = scraper.run()

        # Exit with error code if uploads failed
        if stats['prices_failed'] > 0 and stats['prices_uploaded'] == 0:
            sys.exit(1)

    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
