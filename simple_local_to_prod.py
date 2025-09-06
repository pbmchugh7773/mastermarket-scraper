#!/usr/bin/env python3
"""
Simple Local to Production Scraper
Scrapes using Chrome locally and uploads results to production API
No backend dependencies - completely independent
"""

import time
import json
import logging
import requests
import re
from datetime import datetime
from typing import Optional, Dict, List
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Production API configuration
API_URL = "https://api.mastermarketapp.com"
USERNAME = "testadmin@mastermarket.com" 
PASSWORD = "testadmin123"

class SimpleLocalScraper:
    """
    Simple Chrome-based scraper that uploads to production API
    """
    
    def __init__(self):
        self.driver = None
        self.api_token = None
        self.session = requests.Session()
        
    def authenticate(self) -> bool:
        """Authenticate with production API"""
        try:
            response = self.session.post(
                f'{API_URL}/auth/login',
                data={
                    'username': USERNAME,
                    'password': PASSWORD
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            if response.status_code == 200:
                data = response.json()
                self.api_token = data['access_token']
                self.session.headers['Authorization'] = f'Bearer {self.api_token}'
                logger.info("✅ Authentication successful")
                return True
            else:
                logger.error(f"❌ Authentication failed: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Authentication error: {e}")
            return False
    
    def setup_chrome(self) -> Optional[webdriver.Chrome]:
        """Setup Chrome with anti-detection"""
        try:
            chrome_options = Options()
            
            # Anti-detection options
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--headless")  # Remove this for debugging
            chrome_options.add_argument("--window-size=1920,1080")
            
            # User agent
            chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            
            # Setup driver with webdriver-manager
            # Fix for GitHub Actions - ensure correct chromedriver path
            from webdriver_manager.core.os_manager import ChromeType
            driver_path = ChromeDriverManager(chrome_type=ChromeType.GOOGLE).install()
            
            # On GitHub Actions, the actual chromedriver binary may be in a subdirectory
            import os
            if not os.path.exists(driver_path) or not os.path.isfile(driver_path):
                # Look for the actual chromedriver binary
                possible_paths = [
                    os.path.join(os.path.dirname(driver_path), 'chromedriver'),
                    os.path.join(os.path.dirname(driver_path), 'chromedriver-linux64', 'chromedriver'),
                    driver_path.replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver')
                ]
                for path in possible_paths:
                    if os.path.exists(path) and os.path.isfile(path):
                        driver_path = path
                        break
            
            service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
            # Anti-detection script
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            driver.set_page_load_timeout(60)  # Increased from 20s to 60s
            driver.implicitly_wait(15)     # Increased from 10s to 15s
            
            logger.info("✅ Chrome driver initialized")
            return driver
            
        except Exception as e:
            logger.error(f"❌ Chrome setup failed: {e}")
            return None
    
    def extract_price_from_text(self, text: str) -> Optional[float]:
        """Extract price from text using regex"""
        # European price patterns
        patterns = [
            r'€\s*(\d+[.,]\d{2})',  # €7.25 or €7,25
            r'(\d+[.,]\d{2})\s*€',  # 7.25€ or 7,25€
            r'(\d+[.,]\d{2})',      # Just the number
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                try:
                    price = float(match.replace(',', '.'))
                    if 0.01 <= price <= 1000:  # Reasonable range
                        return price
                except ValueError:
                    continue
        return None
    
    def scrape_aldi(self, url: str, product_name: str) -> Optional[float]:
        """Scrape Aldi product"""
        try:
            logger.info(f"🛒 Scraping Aldi: {product_name}")
            self.driver.get(url)
            time.sleep(3)
            
            # Method 1: JSON-LD structured data
            scripts = self.driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
            for script in scripts:
                try:
                    data = json.loads(script.get_attribute('innerHTML'))
                    if data.get('@type') == 'Product':
                        offers = data.get('offers', {})
                        if isinstance(offers, dict) and offers.get('price'):
                            price = float(offers['price'])
                            logger.info(f"✅ Aldi price via JSON-LD: €{price}")
                            return price
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
            
            # Method 2: CSS selectors
            selectors = [
                '.base-price__regular',
                '.product-price',
                '.price',
                'span[data-testid="price"]',
                'span'
            ]
            
            for selector in selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    text = element.text
                    price = self.extract_price_from_text(text)
                    if price:
                        logger.info(f"✅ Aldi price via selector '{selector}': €{price}")
                        return price
            
            logger.warning(f"⚠️ Could not find Aldi price for {product_name}")
            return None
            
        except Exception as e:
            logger.error(f"❌ Aldi scraping error: {e}")
            return None
    
    def scrape_tesco(self, url: str, product_name: str) -> Optional[float]:
        """Scrape Tesco product"""
        try:
            logger.info(f"🛒 Scraping Tesco: {product_name}")
            self.driver.get(url)
            time.sleep(5)  # Tesco needs more time for JS
            
            # Wait for price elements to load
            try:
                WebDriverWait(self.driver, 90).until(  # Increased from 45s to 90s
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid*='price'], .price, .product-price"))
                )
            except TimeoutException:
                logger.warning("⚠️ Tesco price elements not found, continuing...")
            
            # Tesco-specific selectors
            selectors = [
                '[data-testid="price-details"]',
                '[data-testid*="price"]',
                '.price-per-sellable-unit--now',
                '.ddsweb-product-price__value',
                '.product-price',
                '.price',
                'span[class*="price"]',
                'p[class*="price"]',
                'span'
            ]
            
            for selector in selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    text = element.text
                    if '€' in text or any(char.isdigit() for char in text):
                        price = self.extract_price_from_text(text)
                        if price:
                            logger.info(f"✅ Tesco price via selector '{selector}': €{price}")
                            return price
            
            # Check page source for JavaScript-loaded prices
            page_source = self.driver.page_source
            price_matches = re.findall(r'"price"[:\s]*"?(\d+[.,]\d{2})"?', page_source)
            for match in price_matches:
                try:
                    price = float(match.replace(',', '.'))
                    if 0.01 <= price <= 1000:
                        logger.info(f"✅ Tesco price via page source: €{price}")
                        return price
                except ValueError:
                    continue
            
            logger.warning(f"⚠️ Could not find Tesco price for {product_name}")
            return None
            
        except Exception as e:
            logger.error(f"❌ Tesco scraping error: {e}")
            return None
    
    def scrape_supervalu(self, url: str, product_name: str) -> Optional[float]:
        """Scrape SuperValu product"""
        try:
            logger.info(f"🛒 Scraping SuperValu: {product_name}")
            self.driver.get(url)
            time.sleep(5)
            
            # Wait for product details to load
            try:
                WebDriverWait(self.driver, 45).until(  # Increased from 15s to 45s
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".product-detail, .product-info, .price"))
                )
            except TimeoutException:
                logger.warning("⚠️ SuperValu product elements not found, continuing...")
            
            # Method 1: JSON-LD 
            scripts = self.driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
            for script in scripts:
                try:
                    data = json.loads(script.get_attribute('innerHTML'))
                    
                    # Handle array or single object
                    items = data if isinstance(data, list) else [data]
                    
                    for item in items:
                        if item.get('@type') == 'Product':
                            offers = item.get('offers', {})
                            if isinstance(offers, dict) and offers.get('price'):
                                price = float(offers['price'])
                                logger.info(f"✅ SuperValu price via JSON-LD: €{price}")
                                return price
                                
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
            
            # Method 2: SuperValu-specific selectors
            selectors = [
                '.ProductPrice',
                '.price-now',
                '.PriceText',
                '[data-testid*="price"]',
                '.monetary',
                '.price',
                '.product-price',
                'span[class*="Price"]',
                'span'
            ]
            
            for selector in selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    text = element.text
                    if '€' in text:
                        price = self.extract_price_from_text(text)
                        if price:
                            logger.info(f"✅ SuperValu price via selector '{selector}': €{price}")
                            return price
            
            logger.warning(f"⚠️ Could not find SuperValu price for {product_name}")
            return None
            
        except Exception as e:
            logger.error(f"❌ SuperValu scraping error: {e}")
            return None
    
    def get_product_aliases(self, store_name: str = None, limit: int = 5) -> List[Dict]:
        """Get product aliases from production API"""
        try:
            params = {'limit': limit}
            if store_name:
                params['store_name'] = store_name
            
            response = self.session.get(f'{API_URL}/api/product-aliases/', params=params)
            
            if response.status_code == 200:
                data = response.json()
                aliases = data.get('aliases', [])  # Extract aliases from response
                logger.info(f"✅ Retrieved {len(aliases)} aliases for {store_name}")
                return aliases
            else:
                logger.error(f"❌ Failed to get aliases: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return []
                
        except Exception as e:
            logger.error(f"❌ Error getting aliases: {e}")
            return []
    
    def upload_price(self, alias: Dict, price: float, store_name: str) -> bool:
        """Upload price to production API"""
        try:
            data = {
                'product_id': alias['product_id'],
                'store_name': store_name,
                'store_location': 'IE',  # Ireland location for Irish stores
                'price': price,
                'currency': 'EUR',
                'country': 'IE'
            }
            
            # Use the community prices endpoint
            response = self.session.post(f'{API_URL}/api/community-prices/submit', json=data)
            
            if response.status_code in [200, 201]:
                logger.info(f"✅ Uploaded price for product {alias['product_id']}: €{price}")
                return True
            else:
                logger.warning(f"⚠️ Upload failed for product {alias['product_id']}: {response.status_code}")
                logger.warning(f"Response: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Upload error for product {alias['product_id']}: {e}")
            return False
    
    def scrape_store(self, store_name: str, max_products: int = 5):
        """Scrape all products for a specific store"""
        logger.info(f"\n{'='*50}")
        logger.info(f"🏪 Starting {store_name} scraping (max {max_products} products)")
        logger.info(f"{'='*50}")
        
        # Get product aliases for this store
        aliases = self.get_product_aliases(store_name, max_products)
        if not aliases:
            logger.error(f"No aliases found for {store_name}")
            return
        
        results = []
        
        for i, alias in enumerate(aliases, 1):
            logger.info(f"\n[{i}/{len(aliases)}] Processing: {alias.get('alias_name', 'Unknown')}")
            logger.info(f"URL: {alias['scraper_url'][:80]}...")
            
            start_time = time.time()
            
            # Scrape based on store
            price = None
            if store_name.lower() == 'aldi':
                price = self.scrape_aldi(alias['scraper_url'], alias['alias_name'])
            elif store_name.lower() == 'tesco':
                price = self.scrape_tesco(alias['scraper_url'], alias['alias_name'])
            elif store_name.lower() == 'supervalu':
                price = self.scrape_supervalu(alias['scraper_url'], alias['alias_name'])
            
            elapsed = time.time() - start_time
            
            if price:
                # Upload to production
                success = self.upload_price(alias, price, store_name)
                
                results.append({
                    'alias_id': alias['id'],
                    'name': alias['alias_name'],
                    'price': price,
                    'uploaded': success,
                    'time': elapsed
                })
                
                logger.info(f"✅ Success: €{price:.2f} ({'uploaded' if success else 'upload failed'}) in {elapsed:.2f}s")
            else:
                results.append({
                    'alias_id': alias['id'],
                    'name': alias['alias_name'],
                    'price': None,
                    'uploaded': False,
                    'time': elapsed
                })
                
                logger.warning(f"❌ Failed to extract price in {elapsed:.2f}s")
            
            # Delay between products
            time.sleep(2)
        
        # Summary
        successful = sum(1 for r in results if r['price'] is not None)
        uploaded = sum(1 for r in results if r['uploaded'])
        
        logger.info(f"\n{'='*50}")
        logger.info(f"📊 {store_name} Summary:")
        logger.info(f"   Processed: {len(results)}")
        logger.info(f"   Successful: {successful}")
        logger.info(f"   Uploaded: {uploaded}")
        logger.info(f"   Success Rate: {(successful/len(results)*100):.1f}%")
        logger.info(f"{'='*50}")
    
    def run(self, stores: List[str] = None, max_products: int = 5):
        """Main execution method"""
        if stores is None:
            stores = ['Aldi', 'Tesco', 'SuperValu']
        
        logger.info("🚀 Starting Simple Local to Production Scraper")
        logger.info(f"Target stores: {', '.join(stores)}")
        logger.info(f"Max products per store: {max_products}")
        
        # Authenticate
        if not self.authenticate():
            logger.error("Authentication failed - exiting")
            return
        
        # Setup Chrome
        self.driver = self.setup_chrome()
        if not self.driver:
            logger.error("Chrome setup failed - exiting") 
            return
        
        try:
            # Scrape each store
            for store in stores:
                self.scrape_store(store, max_products)
                
        finally:
            # Cleanup
            if self.driver:
                self.driver.quit()
                logger.info("Chrome driver cleaned up")
        
        logger.info("\n🎉 Scraping completed!")

def main():
    """CLI interface"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Simple Local to Production Scraper')
    parser.add_argument('--store', type=str, help='Store name (Aldi, Tesco, SuperValu)')
    parser.add_argument('--all', action='store_true', help='Scrape all stores')
    parser.add_argument('--products', type=int, default=3, help='Max products per store')
    
    args = parser.parse_args()
    
    # Determine stores
    if args.all:
        stores = ['Aldi', 'Tesco', 'SuperValu'] 
    elif args.store:
        stores = [args.store]
    else:
        # Default: test all stores with fewer products
        stores = ['Aldi', 'Tesco', 'SuperValu']
        args.products = 2
    
    # Run scraper
    scraper = SimpleLocalScraper()
    scraper.run(stores=stores, max_products=args.products)

if __name__ == '__main__':
    main()