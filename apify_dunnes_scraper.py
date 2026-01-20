#!/usr/bin/env python3
"""
Apify-based Dunnes Stores Scraper for MasterMarket.

Uses a custom Apify actor (Crawlee + Puppeteer) to fetch prices from Dunnes Stores
and upload them to MasterMarket API. This bypasses Cloudflare protection that
blocks Selenium in GitHub Actions.

REQUIREMENTS:
1. Create your custom Dunnes actor on Apify (see ACTOR_SETUP.md)
2. Set APIFY_API_TOKEN environment variable
3. Update ACTOR_ID below with your actor's ID

Schedule: Tuesday and Friday (via GitHub Actions)

Usage:
    # Production (default)
    python apify_dunnes_scraper.py

    # With custom limit
    python apify_dunnes_scraper.py --limit 50

    # Dry run (no uploads)
    python apify_dunnes_scraper.py --dry-run

    # Retry mode (only pending/failed products)
    python apify_dunnes_scraper.py --retry-mode
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
from typing import List, Dict, Optional, Any, Tuple

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
# TODO: Update this with your custom actor ID after creating it on Apify
ACTOR_ID = os.getenv('APIFY_DUNNES_ACTOR_ID', 'pbmchugh7773/dunnes-scraper')
STORE_NAME = 'Dunnes Stores'
STORE_LOCATION = 'IE'
CURRENCY = 'EUR'

# Output directory for saving Apify JSON responses
OUTPUT_DIR = Path(__file__).parent / 'output' / 'apify'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class ApifyDunnesScraper:
    """Scraper that uses a custom Apify actor to fetch Dunnes Stores prices."""

    # URL validation pattern for Dunnes Stores (both main site and grocery delivery)
    DUNNES_URL_PATTERN = re.compile(
        r'^https://www\.dunnesstores(grocery)?\.com/.*'
    )

    def __init__(self, dry_run: bool = False, limit: int = None, retry_mode: bool = True):
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
            'promotions_detected': 0
        }

    @classmethod
    def is_valid_dunnes_url(cls, url: str) -> bool:
        """
        Validate if URL is a Dunnes Stores product URL.

        Args:
            url: URL to validate

        Returns:
            True if URL is valid for Dunnes scraper
        """
        if not url:
            return False
        return bool(cls.DUNNES_URL_PATTERN.match(url))

    @staticmethod
    def extract_dunnes_product_id(url: str) -> Optional[str]:
        """
        Extract Dunnes product ID from URL.

        Dunnes URLs typically have a numeric ID at the end:
        - https://www.dunnesstores.com/c/product/123456789

        Returns:
            Product ID as string, or None if not found
        """
        if not url:
            return None

        # Pattern matches digits at the end of URL
        match = re.search(r'/(\d+)(?:[/?#].*)?$', url)
        if match:
            return match.group(1)
        return None

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

    def get_dunnes_aliases(self) -> List[Dict]:
        """Get all Dunnes product aliases with scraper URLs."""
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
        """Get pending aliases that need scraping (retry mode)."""
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
        Run custom Apify Dunnes scraper actor and return results.

        Args:
            urls: List of Dunnes product URLs to scrape

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
            if self.is_valid_dunnes_url(url):
                valid_urls.append(url)
            else:
                invalid_urls.append(url)

        if invalid_urls:
            print(f"  Filtered out {len(invalid_urls)} invalid URL(s):")
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

        # Prepare actor input for custom Dunnes actor
        # The actor should accept these parameters:
        actor_input = {
            "urls": urls,
            "maxConcurrency": 5,  # Conservative to avoid blocking
            "maxRequestRetries": 3,
            "requestHandlerTimeoutSecs": 120,  # Allow time for Cloudflare
            "useResidentialProxies": True,  # Key for bypassing Cloudflare
        }

        print(f"  URLs sample: {urls[:2]}..." if len(urls) > 2 else f"  URLs: {urls}")

        print(f"Starting Apify actor '{ACTOR_ID}' with {len(urls)} URLs...")
        print("This may take 5-20 minutes depending on the number of products...")

        try:
            # Start actor and wait for completion
            run = self.apify_client.actor(ACTOR_ID).call(
                run_input=actor_input,
                timeout_secs=2400  # 40 minutes max (Dunnes is slower due to Cloudflare)
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
        filename = f"apify_dunnes_{timestamp}.json"
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

    def detect_promotion_data(self, item: Dict, current_price: float = None) -> Dict:
        """
        Detect and extract promotion information from Apify result.

        Dunnes Ireland promotion types:
        - Multi-buy: "Buy 3 for 10", "Mix & Match any 3 for 10", "3 for 2"
        - Percentage off: "25% off", "Save 25%"
        - Fixed amount off: "Save 2", "2 off"
        - Was/Now: Original price crossed out with new lower price

        Args:
            item: Product data from Apify actor
            current_price: The current price (for calculating discount)

        Returns:
            dict: Promotion data with keys: original_price, promotion_type,
                  promotion_text, promotion_discount_value
        """
        promotion_data = {
            'original_price': None,
            'promotion_type': None,
            'promotion_text': None,
            'promotion_discount_value': None
        }

        # If the actor already extracted promotion data, use it directly
        # Actor returns camelCase: promotionType, promotionText, etc.
        if item.get('promotionType') or item.get('promotion_type'):
            promotion_data['promotion_type'] = item.get('promotionType') or item.get('promotion_type')
            promotion_data['promotion_text'] = item.get('promotionText') or item.get('promotion_text')
            promotion_data['original_price'] = item.get('originalPrice') or item.get('original_price')
            promotion_data['promotion_discount_value'] = item.get('promotionDiscountValue') or item.get('promotion_discount_value')
            return promotion_data

        # Otherwise, try to parse from raw text fields
        promo_text = (
            item.get('promotionText', '') or
            item.get('promotion', '') or
            item.get('offerText', '') or
            ''
        ).lower()

        html_content = item.get('rawHtml', '') or item.get('pageContent', '') or ''
        html_lower = html_content.lower()

        # Use promo_text if available, otherwise fall back to HTML parsing
        text_to_search = promo_text if promo_text else html_lower

        # === 1. DETECT MULTI-BUY OFFERS (Primary for Dunnes) ===
        multibuy_patterns = [
            # Standard "Buy X for Y" format
            (r'buy\s*(\d+)\s*for\s*[^\d]*(\d+(?:[.,]\d{2})?)', 'buy_x_for'),
            # "Any X for Y" format (Mix & Match style)
            (r'(?:mix\s*&?\s*match\s+)?any\s*(\d+)\s*for\s*[^\d]*(\d+(?:[.,]\d{2})?)', 'any_x_for'),
            # Standard "X for Y" without "buy"
            (r'(\d+)\s*for\s*[^\d]*(\d+(?:[.,]\d{2})?)', 'x_for'),
            # "Buy X get Y free" style
            (r'buy\s*(\d+)\s*get\s*(\d+)\s*free', 'bogo'),
            # "3 for 2" style (buy 3 pay for 2)
            (r'(\d+)\s*for\s*(\d+)\s*\*', 'x_for_y'),
        ]

        for pattern, ptype in multibuy_patterns:
            match = re.search(pattern, text_to_search)
            if match:
                groups = match.groups()

                if ptype == 'buy_x_for' and len(groups) >= 2:
                    qty = groups[0]
                    price = groups[1].replace(',', '.')
                    if '.' not in price:
                        price += '.00'
                    promotion_data['promotion_type'] = 'multi_buy'
                    promotion_data['promotion_text'] = f'Buy {qty} for {price}'
                    return promotion_data

                elif ptype == 'any_x_for' and len(groups) >= 2:
                    qty = groups[0]
                    price = groups[1].replace(',', '.')
                    if '.' not in price:
                        price += '.00'
                    promotion_data['promotion_type'] = 'multi_buy'
                    promotion_data['promotion_text'] = f'Any {qty} for {price}'
                    return promotion_data

                elif ptype == 'x_for' and len(groups) >= 2:
                    qty = groups[0]
                    price = groups[1].replace(',', '.')
                    if '.' not in price:
                        price += '.00'
                    promotion_data['promotion_type'] = 'multi_buy'
                    promotion_data['promotion_text'] = f'{qty} for {price}'
                    return promotion_data

                elif ptype == 'bogo' and len(groups) >= 2:
                    buy_qty = groups[0]
                    free_qty = groups[1]
                    promotion_data['promotion_type'] = 'multi_buy'
                    promotion_data['promotion_text'] = f'Buy {buy_qty} Get {free_qty} Free'
                    return promotion_data

                elif ptype == 'x_for_y' and len(groups) >= 2:
                    qty_buy = groups[0]
                    qty_pay = groups[1]
                    promotion_data['promotion_type'] = 'multi_buy'
                    promotion_data['promotion_text'] = f'Buy {qty_buy} for {qty_pay}'
                    return promotion_data

        # === 2. DETECT PERCENTAGE DISCOUNTS ===
        percentage_patterns = [
            r'(\d+)\s*%\s*off',
            r'save\s*(\d+)\s*%',
            r'(\d+)\s*%\s*discount',
            r'half\s*price',
        ]

        for pattern in percentage_patterns:
            match = re.search(pattern, text_to_search)
            if match:
                if 'half' in pattern:
                    promotion_data['promotion_type'] = 'percentage_off'
                    promotion_data['promotion_text'] = 'Half Price'
                    promotion_data['promotion_discount_value'] = 50.0
                    return promotion_data
                else:
                    try:
                        discount_pct = float(match.group(1))
                        if 0 < discount_pct <= 90:
                            promotion_data['promotion_type'] = 'percentage_off'
                            promotion_data['promotion_text'] = f'{int(discount_pct)}% Off'
                            promotion_data['promotion_discount_value'] = discount_pct
                            return promotion_data
                    except (ValueError, IndexError):
                        continue

        # === 3. DETECT FIXED AMOUNT SAVINGS ===
        savings_patterns = [
            r'save\s*[^\d]*(\d+[.,]\d{2})',
            r'[^\d](\d+[.,]\d{2})\s*off',
            r'saving\s*[^\d]*(\d+[.,]\d{2})',
        ]

        for pattern in savings_patterns:
            match = re.search(pattern, text_to_search)
            if match:
                try:
                    discount_amount = float(match.group(1).replace(',', '.'))
                    if 0 < discount_amount < 100:
                        promotion_data['promotion_type'] = 'fixed_amount_off'
                        promotion_data['promotion_text'] = f'Save {discount_amount:.2f}'
                        promotion_data['promotion_discount_value'] = discount_amount
                        return promotion_data
                except ValueError:
                    continue

        # === 4. DETECT "WAS/NOW" PRICING ===
        # Check for original price from the actor data first
        original_price = item.get('originalPrice') or item.get('wasPrice')
        if original_price and current_price:
            try:
                original_price = float(original_price)
                if original_price > current_price:
                    promotion_data['original_price'] = original_price
                    promotion_data['promotion_type'] = 'temporary_discount'
                    promotion_data['promotion_text'] = f'Was {original_price:.2f}'
                    promotion_data['promotion_discount_value'] = round(original_price - current_price, 2)
                    return promotion_data
            except (TypeError, ValueError):
                pass

        # Parse from text
        was_patterns = [
            r'was\s*[^\d]*(\d+[.,]\d{2})',
            r'original\s*price[:\s]*[^\d]*(\d+[.,]\d{2})',
        ]

        for pattern in was_patterns:
            match = re.search(pattern, text_to_search)
            if match:
                try:
                    original_price = float(match.group(1).replace(',', '.'))
                    if current_price and original_price > current_price:
                        promotion_data['original_price'] = original_price
                        promotion_data['promotion_type'] = 'temporary_discount'
                        promotion_data['promotion_text'] = f'Was {original_price:.2f}'
                        promotion_data['promotion_discount_value'] = round(original_price - current_price, 2)
                        return promotion_data
                    elif original_price > 0 and not current_price:
                        promotion_data['original_price'] = original_price
                        promotion_data['promotion_type'] = 'temporary_discount'
                        promotion_data['promotion_text'] = f'Was {original_price:.2f}'
                        return promotion_data
                except ValueError:
                    continue

        return promotion_data

    def extract_price_data(self, item: Dict) -> Optional[Dict]:
        """
        Extract price and promotion data from Apify result.

        Args:
            item: Single product result from Apify

        Returns:
            Dict with price data or None if invalid
        """
        # Try different field names for price
        price = (
            item.get('price') or
            item.get('currentPrice') or
            item.get('salePrice') or
            item.get('regularPrice')
        )

        if not price:
            # Try to extract from text
            price_text = item.get('priceText', '')
            if price_text:
                match = re.search(r'(\d+[.,]\d{2})', price_text)
                if match:
                    try:
                        price = float(match.group(1).replace(',', '.'))
                    except ValueError:
                        pass

        if not price:
            return None

        # Ensure price is a float
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None

        # Validate price range (0.01 - 1000)
        if price < 0.01 or price > 1000:
            return None

        result = {
            'price': price,
            'original_price': price,  # Default to same as price
            'url': item.get('url', ''),
            'title': item.get('title', '') or item.get('name', ''),
        }

        # Detect promotions
        promotion_data = self.detect_promotion_data(item, price)

        if promotion_data.get('promotion_type'):
            result['promotion_type'] = promotion_data['promotion_type']
            result['promotion_text'] = promotion_data['promotion_text']
            if promotion_data.get('original_price'):
                result['original_price'] = promotion_data['original_price']
            if promotion_data.get('promotion_discount_value'):
                result['promotion_discount_value'] = promotion_data['promotion_discount_value']
            self.stats['promotions_detected'] += 1

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
            promo_info = f", promo={price_data.get('promotion_type')}" if price_data.get('promotion_type') else ""
            print(f"  [DRY RUN] Would upload: product_id={product_id}, price={price_data['price']}{promo_info}")
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
        if 'promotion_type' in price_data:
            # Map temporary_discount to 'other' (not in backend enum)
            promo_type = price_data['promotion_type']
            if promo_type == 'temporary_discount':
                promo_type = 'other'
            payload['promotion_type'] = promo_type
        if 'promotion_text' in price_data:
            payload['promotion_text'] = price_data['promotion_text']
        if 'original_price' in price_data:
            payload['original_price'] = price_data['original_price']
        if 'promotion_discount_value' in price_data:
            payload['promotion_discount_value'] = price_data['promotion_discount_value']

        promo_info = f", promo_type={payload.get('promotion_type')}" if payload.get('promotion_type') else ""
        print(f"    Uploading: product_id={product_id}, price={payload['price']}{promo_info}")

        # Retry logic
        for attempt in range(3):
            try:
                response = self.session.post(
                    f"{API_URL}/api/community-prices/submit-scraped",
                    json=payload,
                    timeout=30
                )

                if response.status_code in (200, 201):
                    print(f"    API response: {response.text[:200]}")
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
        """Update scraping status for an alias."""
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
        print(f"Apify Dunnes Stores Scraper - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Mode: {'DRY RUN' if self.dry_run else 'PRODUCTION'}")
        if self.retry_mode:
            print(f"Retry Mode: ENABLED (only pending/failed aliases)")
        print(f"API: {API_URL}")
        print(f"Actor: {ACTOR_ID}")
        print(f"{'='*60}\n")

        # Warn if using placeholder actor ID
        if 'YOUR_USERNAME' in ACTOR_ID:
            print("WARNING: Using placeholder actor ID. Please update ACTOR_ID or set APIFY_DUNNES_ACTOR_ID")
            print("See the plan documentation for instructions on creating the custom actor.\n")

        # Step 1: Authenticate with MasterMarket
        print("[1/4] Authenticating with MasterMarket...")
        if not self.authenticate_mastermarket():
            print("ERROR: Failed to authenticate with MasterMarket")
            return self.stats
        print("  Authenticated successfully")

        # Step 2: Get Dunnes aliases (all or pending based on retry_mode)
        if self.retry_mode:
            print("\n[2/4] Fetching pending Dunnes aliases (retry mode)...")
            aliases = self.get_pending_aliases()
            if not aliases:
                print("  No pending aliases found - all products up to date!")
                return self.stats
        else:
            print("\n[2/4] Fetching Dunnes product aliases...")
            aliases = self.get_dunnes_aliases()

        # Build mappings for matching
        url_to_product = {}
        dunnes_id_to_product = {}
        url_to_alias_id = {}

        for alias in aliases:
            scraper_url = alias.get('scraper_url')
            product_id = alias.get('product_id')
            alias_id = alias.get('id')
            if scraper_url and product_id:
                url_to_product[scraper_url] = product_id
                if alias_id:
                    url_to_alias_id[scraper_url] = alias_id

                # Also map by Dunnes product ID for fallback matching
                dunnes_id = self.extract_dunnes_product_id(scraper_url)
                if dunnes_id:
                    dunnes_id_to_product[dunnes_id] = product_id

        urls = list(url_to_product.keys())
        print(f"  Found {len(aliases)} aliases, {len(urls)} with scraper URLs")

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

            # Strategy 1: Direct URL match
            product_id = url_to_product.get(url)
            if product_id:
                self.stats['matched_by_url'] += 1

            # Strategy 2: Match by Dunnes product ID
            if not product_id:
                dunnes_id = self.extract_dunnes_product_id(url)
                if dunnes_id and dunnes_id in dunnes_id_to_product:
                    product_id = dunnes_id_to_product[dunnes_id]

            # Strategy 3: Partial URL match (fallback)
            if not product_id:
                for stored_url, pid in url_to_product.items():
                    if url in stored_url or stored_url in url:
                        product_id = pid
                        break

            if not product_id:
                self.stats['prices_skipped'] += 1
                continue

            # Get alias_id for this URL
            alias_id = url_to_alias_id.get(url)

            # Extract price data
            price_data = self.extract_price_data(item)
            if not price_data:
                self.stats['prices_skipped'] += 1
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
        print(f"  Promotions detected:  {self.stats['promotions_detected']}")
        print(f"  Prices uploaded:      {self.stats['prices_uploaded']}")
        print(f"  Prices failed:        {self.stats['prices_failed']}")
        print(f"  Prices skipped:       {self.stats['prices_skipped']}")
        if self.stats.get('urls_filtered'):
            print(f"  URLs filtered:        {self.stats['urls_filtered']}")
        print(f"  Elapsed time:         {elapsed:.1f} seconds")
        if self.stats.get('json_saved'):
            print(f"  JSON saved to:        {self.stats['json_saved']}")
        print(f"{'='*60}\n")

        return self.stats


def main():
    parser = argparse.ArgumentParser(
        description="Apify-based Dunnes Stores Scraper for MasterMarket",
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
        default=True,
        help='Only scrape pending/failed aliases (default: True)'
    )

    parser.add_argument(
        '--all',
        action='store_true',
        help='Scrape all aliases, not just pending ones'
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
        # --all flag disables retry_mode
        retry_mode = not args.all
        scraper = ApifyDunnesScraper(
            dry_run=args.dry_run,
            limit=args.limit,
            retry_mode=retry_mode
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
