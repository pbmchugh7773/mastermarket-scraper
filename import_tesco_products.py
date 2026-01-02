#!/usr/bin/env python3
"""
Tesco Product Importer for MasterMarket

Imports products from Apify Tesco Scraper JSON files into MasterMarket
via the backend API.

Usage:
    python import_tesco_products.py <json_file> [--dry-run] [--limit N]

Environment Variables:
    MASTERMARKET_API_URL - API base URL (default: http://localhost:8000)
    MASTERMARKET_EMAIL - Admin email for authentication
    MASTERMARKET_PASSWORD - Admin password for authentication
"""

import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f'import_tesco_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    ]
)
logger = logging.getLogger(__name__)


class TescoImporter:
    """Imports Tesco products from JSON to MasterMarket API."""

    STORE_NAME = "Tesco"
    STORE_LOCATION = "IE"  # Must match country code for filtering
    CURRENCY = "EUR"
    COUNTRY = "IE"

    def __init__(self, api_url: str, email: str, password: str, dry_run: bool = False):
        self.api_url = api_url.rstrip('/')
        self.email = email
        self.password = password
        self.dry_run = dry_run
        self.token: Optional[str] = None
        self.session = requests.Session()

        # Statistics
        self.stats = {
            'total': 0,
            'created': 0,
            'skipped': 0,
            'prices_created': 0,
            'errors': 0,
        }

    def authenticate(self) -> bool:
        """Authenticate with the API and get access token."""
        logger.info(f"Authenticating with {self.api_url}...")

        try:
            response = self.session.post(
                f"{self.api_url}/auth/login",
                data={
                    "username": self.email,
                    "password": self.password
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

            if response.status_code == 200:
                data = response.json()
                self.token = data.get('access_token')
                self.session.headers.update({
                    "Authorization": f"Bearer {self.token}"
                })
                logger.info("Authentication successful!")
                return True
            else:
                logger.error(f"Authentication failed: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False

    def normalize_barcode(self, gtin: str) -> str:
        """
        Normalize GTIN to standard format (GTIN-13 or GTIN-8).

        - GTINs with 14+ digits → strip leading zeros
        - Result with ≤8 digits → format as GTIN-8 (8 digits with leading zeros)
        - Result with 9-13 digits → keep as GTIN-13

        Examples:
            "05011100650236" -> "5011100650236" (GTIN-13)
            "00000003341315" -> "03341315" (GTIN-8, preserves leading zero)
            "00261480000000" -> "261480000000" (GTIN-13)
            "0000000054491014" -> "54491014" (GTIN-8)
        """
        if not gtin:
            return ""

        # Strip leading zeros
        stripped = gtin.lstrip('0') or '0'

        # Determine correct format
        if len(stripped) <= 8:
            # GTIN-8: pad with leading zeros to 8 digits
            return stripped.zfill(8)
        elif len(stripped) <= 13:
            # GTIN-13: no additional padding needed
            return stripped
        else:
            # More than 13 digits: return as-is
            return stripped

    def product_exists(self, barcode: str) -> Tuple[bool, Optional[int]]:
        """
        Check if a product exists by barcode.

        Returns:
            (exists: bool, product_id: int or None)
        """
        try:
            response = self.session.get(
                f"{self.api_url}/products/barcode/{barcode}"
            )

            if response.status_code == 200:
                data = response.json()
                # API returns a list of products
                if isinstance(data, list) and len(data) > 0:
                    return True, data[0].get('id')
                elif isinstance(data, dict) and data.get('id'):
                    return True, data.get('id')

            return False, None

        except Exception as e:
            logger.warning(f"Error checking barcode {barcode}: {e}")
            return False, None

    def create_product(self, item: Dict[str, Any], barcode: str) -> Optional[int]:
        """
        Create a new product from Tesco JSON item.

        Returns:
            Product ID if created, None if failed
        """
        product_data = {
            "name": item.get('name', ''),
            "description": item.get('description') or item.get('name', ''),
            "category": item.get('product_category') or item.get('sub_category') or 'Other',
            "brand": item.get('brand_name') or '',
            "barcode": barcode,
            "image_url": item.get('image_url') or '',
            "quantity": 1,
            "unit": item.get('unit') or None,
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would create product: {product_data['name']}")
            return -1  # Fake ID for dry run

        try:
            response = self.session.post(
                f"{self.api_url}/admin/products",
                json=product_data
            )

            if response.status_code in (200, 201):
                data = response.json()
                # API returns {'product': {'id': ...}} structure
                product_id = data.get('product', {}).get('id') or data.get('id')
                logger.info(f"Created product ID {product_id}: {product_data['name']}")
                return product_id
            else:
                logger.error(f"Failed to create product: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"Error creating product: {e}")
            return None

    def create_alias(self, product_id: int, item: Dict[str, Any]) -> bool:
        """
        Create a product alias for Tesco.

        Returns:
            True if created successfully
        """
        alias_data = {
            "product_id": product_id,
            "store_name": self.STORE_NAME,
            "alias_name": item.get('name', ''),
            "scraper_url": item.get('url') or '',
            "is_primary": True,
            "confidence_score": 1.0,
            "is_active_for_scraping": True,
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Would create alias: {alias_data['alias_name']}")
            return True

        try:
            response = self.session.post(
                f"{self.api_url}/api/product-aliases/",
                json=alias_data
            )

            if response.status_code in (200, 201):
                logger.info(f"Created alias for product {product_id}")
                return True
            elif response.status_code == 400 and 'unique' in response.text.lower():
                logger.warning(f"Alias already exists for product {product_id}")
                return True  # Not an error, just already exists
            else:
                logger.error(f"Failed to create alias: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error creating alias: {e}")
            return False

    def submit_price(self, product_id: int, item: Dict[str, Any]) -> bool:
        """
        Submit a price for the product.

        Returns:
            True if submitted successfully
        """
        price_data = {
            "product_id": product_id,
            "store_name": self.STORE_NAME,
            "store_location": self.STORE_LOCATION,
            "price": float(item.get('price', 0)),
            "currency": self.CURRENCY,
            "country": self.COUNTRY,
        }

        # Add promotion info if available
        if item.get('promotion'):
            promo_text = str(item.get('promotion'))
            price_data['promotion_text'] = promo_text

            # Detect correct promotion_type based on text
            promo_lower = promo_text.lower()
            if 'clubcard' in promo_lower:
                # Check for multi-buy patterns (e.g., "Any 2 for €11")
                if any(pattern in promo_lower for pattern in ['any ', ' for €', ' for £', '2 for', '3 for']):
                    price_data['promotion_type'] = 'multi_buy'
                else:
                    price_data['promotion_type'] = 'membership_price'
            elif any(pattern in promo_lower for pattern in ['any ', ' for €', ' for £', '2 for', '3 for']):
                price_data['promotion_type'] = 'multi_buy'
            elif '%' in promo_lower or 'percent' in promo_lower:
                price_data['promotion_type'] = 'percentage_off'
            else:
                price_data['promotion_type'] = 'other'

        if self.dry_run:
            logger.info(f"[DRY RUN] Would submit price: {price_data['price']} {price_data['currency']}")
            return True

        try:
            response = self.session.post(
                f"{self.api_url}/api/community-prices/submit-scraped",
                json=price_data
            )

            if response.status_code in (200, 201):
                logger.info(f"Submitted price {price_data['price']} EUR for product {product_id}")
                return True
            else:
                logger.error(f"Failed to submit price: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error submitting price: {e}")
            return False

    def process_item(self, item: Dict[str, Any]) -> bool:
        """
        Process a single item from the JSON.

        Returns:
            True if processed successfully
        """
        gtin = item.get('gtin', '')
        if not gtin:
            logger.warning(f"Skipping item without GTIN: {item.get('name', 'Unknown')}")
            return False

        barcode = self.normalize_barcode(gtin)
        name = item.get('name', 'Unknown')

        logger.info(f"Processing: {name} (barcode: {barcode})")

        # Check if product exists
        exists, product_id = self.product_exists(barcode)

        if exists:
            logger.info(f"Product already exists (ID: {product_id}), skipping creation")
            self.stats['skipped'] += 1
        else:
            # Create product
            product_id = self.create_product(item, barcode)
            if product_id is None:
                self.stats['errors'] += 1
                return False

            # Create alias
            if not self.create_alias(product_id, item):
                logger.warning(f"Failed to create alias for product {product_id}")

            self.stats['created'] += 1

        # Submit price (always, even for existing products)
        if product_id and product_id != -1:  # -1 is dry run fake ID
            if self.submit_price(product_id, item):
                self.stats['prices_created'] += 1
        elif self.dry_run:
            self.stats['prices_created'] += 1

        return True

    def import_from_json(self, json_path: str, limit: Optional[int] = None) -> Dict[str, int]:
        """
        Import products from a JSON file.

        Args:
            json_path: Path to the JSON file
            limit: Maximum number of items to process (None = all)

        Returns:
            Statistics dictionary
        """
        logger.info(f"Loading JSON from {json_path}...")

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                products = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load JSON: {e}")
            return self.stats

        if not isinstance(products, list):
            logger.error("JSON file must contain an array of products")
            return self.stats

        total_items = len(products)
        if limit:
            products = products[:limit]

        logger.info(f"Processing {len(products)} of {total_items} products...")

        for i, item in enumerate(products, 1):
            self.stats['total'] += 1

            try:
                self.process_item(item)
            except Exception as e:
                logger.error(f"Error processing item {i}: {e}")
                self.stats['errors'] += 1

            # Rate limiting - small delay between requests
            if not self.dry_run:
                time.sleep(0.2)

            # Progress update every 10 items
            if i % 10 == 0:
                logger.info(f"Progress: {i}/{len(products)} ({i*100//len(products)}%)")

        return self.stats

    def print_summary(self):
        """Print import summary statistics."""
        print("\n" + "="*50)
        print("IMPORT SUMMARY")
        print("="*50)
        print(f"Total processed:  {self.stats['total']}")
        print(f"Products created: {self.stats['created']}")
        print(f"Products skipped: {self.stats['skipped']}")
        print(f"Prices created:   {self.stats['prices_created']}")
        print(f"Errors:           {self.stats['errors']}")
        print("="*50)


def main():
    parser = argparse.ArgumentParser(
        description='Import Tesco products from JSON to MasterMarket'
    )
    parser.add_argument(
        'json_file',
        help='Path to the Tesco scraper JSON file'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run without making any changes'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Limit number of products to import'
    )
    parser.add_argument(
        '--api-url',
        default=os.environ.get('MASTERMARKET_API_URL', 'http://localhost:8000'),
        help='MasterMarket API URL'
    )

    args = parser.parse_args()

    # Get credentials from environment
    email = os.environ.get('MASTERMARKET_EMAIL', 'testadmin@mastermarket.com')
    password = os.environ.get('MASTERMARKET_PASSWORD', 'testadmin123')

    # Validate JSON file exists
    if not os.path.exists(args.json_file):
        logger.error(f"JSON file not found: {args.json_file}")
        sys.exit(1)

    # Create importer
    importer = TescoImporter(
        api_url=args.api_url,
        email=email,
        password=password,
        dry_run=args.dry_run
    )

    if args.dry_run:
        logger.info("*** DRY RUN MODE - No changes will be made ***")

    # Authenticate
    if not args.dry_run:
        if not importer.authenticate():
            logger.error("Failed to authenticate. Exiting.")
            sys.exit(1)

    # Import products
    importer.import_from_json(args.json_file, limit=args.limit)

    # Print summary
    importer.print_summary()


if __name__ == '__main__':
    main()
