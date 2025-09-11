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
import os
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

# Production API configuration with environment variable support
API_URL = os.getenv('API_URL', 'https://api.mastermarketapp.com')
USERNAME = os.getenv('SCRAPER_USERNAME', 'pricerIE@mastermarket.com')
PASSWORD = os.getenv('SCRAPER_PASSWORD', 'pricerIE')

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
            
            # Enhanced anti-detection for Cloudflare and GitHub Actions
            chrome_options.add_argument("--disable-web-security")
            chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")
            chrome_options.add_argument("--allow-running-insecure-content")
            chrome_options.add_argument("--disable-features=VizDisplayCompositor")
            
            # Additional stealth options for GitHub Actions
            chrome_options.add_argument("--disable-background-timer-throttling")
            chrome_options.add_argument("--disable-backgrounding-occluded-windows")
            chrome_options.add_argument("--disable-renderer-backgrounding")
            chrome_options.add_argument("--disable-features=TranslateUI")
            chrome_options.add_argument("--no-first-run")
            chrome_options.add_argument("--no-default-browser-check")
            chrome_options.add_argument("--disable-logging")
            chrome_options.add_argument("--disable-plugins-discovery")
            chrome_options.add_argument("--disable-ipc-flooding-protection")
            
            # Use smaller viewport (mobile-ish but not full mobile emulation)
            import random
            width = random.randint(360, 414)  # Mobile-like width
            height = random.randint(640, 896)  # Mobile-like height
            chrome_options.add_argument(f"--window-size={width},{height}")
            
            # Use mobile user agents to avoid Cloudflare (less aggressive on mobile)
            user_agents = [
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
                "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
            ]
            user_agent = random.choice(user_agents)
            chrome_options.add_argument(f"--user-agent={user_agent}")
            
            logger.info(f"Using viewport: {width}x{height}")
            logger.info(f"Using user agent: {user_agent[:50]}...")
            
            # Setup driver - GitHub Actions compatible approach
            import os
            
            # Try to use system Chrome driver first (GitHub Actions has Chrome pre-installed)
            system_chromedriver = '/usr/bin/chromedriver'
            if os.path.exists(system_chromedriver):
                logger.info("Using system ChromeDriver from GitHub Actions")
                service = Service(system_chromedriver)
            else:
                # Fallback to webdriver-manager with proper path handling
                from webdriver_manager.core.os_manager import ChromeType
                base_path = ChromeDriverManager(chrome_type=ChromeType.GOOGLE).install()
                
                # Find the actual chromedriver executable
                import glob
                chromedriver_pattern = os.path.join(os.path.dirname(base_path), '**/chromedriver')
                possible_drivers = glob.glob(chromedriver_pattern, recursive=True)
                
                driver_path = None
                for path in possible_drivers:
                    if os.path.isfile(path) and os.access(path, os.X_OK):
                        driver_path = path
                        logger.info(f"Found executable ChromeDriver at: {path}")
                        break
                
                if not driver_path:
                    # Last resort: try common locations
                    common_paths = [
                        '/usr/local/bin/chromedriver',
                        '/usr/bin/chromedriver',
                        base_path.replace('THIRD_PARTY_NOTICES.chromedriver', 'chromedriver'),
                        os.path.join(os.path.dirname(base_path), 'chromedriver-linux64', 'chromedriver')
                    ]
                    
                    for path in common_paths:
                        if os.path.exists(path) and os.path.isfile(path):
                            driver_path = path
                            break
                
                if not driver_path:
                    raise Exception(f"Could not find ChromeDriver executable. Checked paths: {possible_drivers + common_paths}")
                
                service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
            # Enhanced anti-detection scripts
            stealth_script = """
                // Hide webdriver property
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                
                // Override the `plugins` property to use a custom getter.
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // Override the `languages` property to use a custom getter.
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
                
                // Override the `permissions` property to use a custom getter.
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """
            
            driver.execute_script(stealth_script)
            
            driver.set_page_load_timeout(90)  # Increased for Cloudflare
            driver.implicitly_wait(10)
            
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
        """Scrape Aldi product - optimized for speed"""
        try:
            logger.info(f"🛒 Scraping Aldi: {product_name}")
            self.driver.get(url)
            
            # Quick initial check - often price is available immediately
            time.sleep(1)  # Reduced from 3s to 1s
            
            # Method 1: JSON-LD structured data (fastest)
            scripts = self.driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
            for script in scripts[:3]:  # Only check first 3 scripts
                try:
                    data = json.loads(script.get_attribute('innerHTML'))
                    if data.get('@type') == 'Product':
                        offers = data.get('offers', {})
                        if isinstance(offers, dict) and offers.get('price'):
                            price = float(offers['price'])
                            if 0.01 <= price <= 1000:
                                logger.info(f"✅ Aldi price via JSON-LD: €{price}")
                                return price
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
            
            # Method 2: Prioritized CSS selectors
            priority_selectors = [
                '.base-price__regular',
                'span[data-testid="price"]',
                '.product-price'
            ]
            
            fallback_selectors = [
                '.price',
                'span'
            ]
            
            # Try priority selectors first
            for selector in priority_selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements[:3]:  # Only first 3 elements
                    text = element.text.strip()
                    if text:
                        price = self.extract_price_from_text(text)
                        if price:
                            logger.info(f"✅ Aldi price via priority selector '{selector}': €{price}")
                            return price
            
            # Only try fallback if priority failed
            for selector in fallback_selectors:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements[:5]:  # Limit to 5 for generic selectors
                    text = element.text.strip()
                    if text and ('€' in text or any(char.isdigit() for char in text)):
                        price = self.extract_price_from_text(text)
                        if price:
                            logger.info(f"✅ Aldi price via fallback selector '{selector}': €{price}")
                            return price
            
            logger.warning(f"⚠️ Could not find Aldi price for {product_name}")
            return None
            
        except Exception as e:
            logger.error(f"❌ Aldi scraping error: {e}")
            return None
    
    def scrape_tesco(self, url: str, product_name: str) -> Optional[float]:
        """Scrape Tesco product with enhanced anti-detection"""
        start_time = time.time()
        max_time = 60  # Increased timeout for complex loading
        
        try:
            logger.info(f"🛒 Scraping Tesco: {product_name}")
            
            # Enhanced anti-detection setup
            self.driver.set_page_load_timeout(30)
            
            # Add simple stealth measures
            try:
                self.driver.execute_script("window.chrome = {runtime: {}};")
            except:
                pass
            
            # Navigate with error handling
            try:
                logger.info("🔄 Loading Tesco page with enhanced stealth...")
                self.driver.get(url)
            except Exception as e:
                logger.warning(f"⚠️ Tesco page load issue: {e}")
                # Don't return yet, sometimes partial loads work
            
            # Check if we got an error page
            page_title = self.driver.title
            if "Error" in page_title or page_title == "Error":
                logger.warning("⚠️ Tesco returned error page - possible bot detection")
                # Try one more time with additional delays
                time.sleep(3)
                
                try:
                    logger.info("🔄 Retrying Tesco page load...")
                    self.driver.refresh()
                    time.sleep(5)
                except:
                    pass
                
                # Check again
                page_title = self.driver.title
                if "Error" in page_title:
                    logger.warning("❌ Tesco blocked Selenium access - trying requests fallback")
                    return self._scrape_tesco_requests_fallback(url, product_name)
            
            # Wait for dynamic content
            time.sleep(8)  # Increased from 5s to 8s
            
            # Check time limit
            if time.time() - start_time > max_time:
                return None

            # === 1. JSON-LD FIRST (highest priority) ===
            try:
                page_source = self.driver.page_source
                logger.info(f"🔍 Searching JSON-LD in page source ({len(page_source)} chars)")
                
                import re
                json_pattern = r'<script type="application/ld\+json"[^>]*>(.*?)</script>'
                json_matches = re.findall(json_pattern, page_source, re.DOTALL | re.IGNORECASE)
                logger.info(f"🔍 Found {len(json_matches)} JSON-LD patterns in source")
                
                for i, json_content in enumerate(json_matches[:3]):
                    try:
                        logger.info(f"  📄 JSON pattern {i+1} preview: {json_content[:200]}...")
                        data = json.loads(json_content)
                        
                        # Handle @graph structure (common in Tesco)
                        if isinstance(data, dict) and '@graph' in data:
                            items = data['@graph']
                        else:
                            items = data if isinstance(data, list) else [data]
                        
                        for item in items:
                            if isinstance(item, dict) and item.get('@type') == 'Product':
                                logger.info(f"    🎯 Found Product in JSON-LD!")
                                offers = item.get('offers', {})
                                if isinstance(offers, dict) and 'price' in offers:
                                    try:
                                        price = float(offers['price'])
                                        logger.info(f"    💰 Extracted price: {price}")
                                        if 0.01 <= price <= 1000:
                                            elapsed = time.time() - start_time
                                            logger.info(f"✅ Tesco price via JSON-LD: €{price} (in {elapsed:.1f}s)")
                                            return price
                                    except (ValueError, TypeError) as e:
                                        logger.warning(f"    ⚠️ Price conversion error: {e}")
                                        continue
                    except Exception as pattern_e:
                        logger.warning(f"    ⚠️ JSON pattern {i+1} error: {pattern_e}")
                        continue
                        
            except Exception as json_e:
                logger.warning(f"  ⚠️ JSON-LD search error: {json_e}")

            # Check time limit before continuing
            if time.time() - start_time > max_time:
                return None

            # === 2. Key CSS selectors only (limited) ===
            priority_selectors = [
                '[data-testid="price-details"]',
                '[data-testid*="price"]',
                '.ddsweb-product-price__value'
            ]
            
            # Try only key selectors (quick check)
            for selector in priority_selectors:
                if time.time() - start_time > max_time:
                    break
                    
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    logger.info(f"🔍 Selector '{selector}': found {len(elements)} elements")
                    
                    for i, element in enumerate(elements[:2]):  # Only check first 2 elements
                        try:
                            text = element.text.strip()
                            if text and ('€' in text or any(char.isdigit() for char in text)):
                                price = self.extract_price_from_text(text)
                                if price:
                                    elapsed = time.time() - start_time
                                    logger.info(f"✅ Tesco price via selector '{selector}': €{price} (in {elapsed:.1f}s)")
                                    return price
                        except Exception:
                            continue
                            
                except Exception:
                    continue

            # === 3. Final regex check ===
            if time.time() - start_time < max_time:
                try:
                    page_source = self.driver.page_source
                    logger.info(f"🔍 Searching page source ({len(page_source)} chars)")
                    
                    # More comprehensive price patterns
                    price_patterns = [
                        r'"price"\s*[:=]\s*"?(\d+[.,]\d{2})"?',
                        r'€\s*(\d+[.,]\d{2})',
                        r'(\d+[.,]\d{2})\s*€',
                        r'"amount"\s*[:=]\s*"?(\d+[.,]\d{2})"?',
                        r'price["\s:]*(\d+[.,]\d{2})',
                        r'"currentPrice"[:\s]*"?(\d+[.,]\d{2})"?',
                        r'"sellPrice"[:\s]*"?(\d+[.,]\d{2})"?',
                        r'"priceNow"[:\s]*"?(\d+[.,]\d{2})"?',
                    ]
                    
                    for pattern in price_patterns:
                        matches = re.findall(pattern, page_source, re.IGNORECASE)
                        logger.info(f"  🔍 Pattern '{pattern[:30]}...': {len(matches)} matches")
                        
                        for match in matches[:10]:  # Check first 10 matches
                            try:
                                price = float(match.replace(',', '.'))
                                if 0.01 <= price <= 1000:
                                    elapsed = time.time() - start_time
                                    logger.info(f"✅ Tesco price via regex: €{price} (in {elapsed:.1f}s)")
                                    return price
                            except ValueError:
                                continue
                                
                except Exception as regex_e:
                    logger.debug(f"  ⚠️ Regex error: {regex_e}")

            elapsed = time.time() - start_time
            logger.warning(f"⚠️ Could not find Tesco price for {product_name} after comprehensive search ({elapsed:.1f}s)")
            
            # Debug: Save page source for analysis
            try:
                with open(f"tesco_debug_{int(time.time())}.html", "w") as f:
                    f.write(self.driver.page_source)
                logger.info("💾 Saved page source for debugging")
            except:
                pass
            
            return None

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"❌ Tesco scraping error after {elapsed:.1f}s: {e}")
            return None
        finally:
            # Reset page load timeout
            try:
                self.driver.set_page_load_timeout(90)
            except:
                pass
    
    def _scrape_tesco_requests_fallback(self, url: str, product_name: str) -> Optional[float]:
        """Fallback method using requests instead of Selenium for Tesco"""
        try:
            logger.info("🔄 Trying Tesco requests fallback method...")
            
            # Mobile-like headers to avoid detection
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none'
            }
            
            # Make request with timeout
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                html_content = response.text
                logger.info(f"✅ Successfully fetched Tesco page with requests ({len(html_content)} chars)")
                
                # Look for JSON-LD in the HTML
                import re
                json_pattern = r'<script type="application/ld\+json"[^>]*>(.*?)</script>'
                json_matches = re.findall(json_pattern, html_content, re.DOTALL | re.IGNORECASE)
                logger.info(f"🔍 Found {len(json_matches)} JSON-LD patterns via requests")
                
                for i, json_content in enumerate(json_matches[:3]):
                    try:
                        logger.info(f"  📄 JSON pattern {i+1} preview: {json_content[:100]}...")
                        data = json.loads(json_content)
                        
                        # Handle @graph structure
                        if isinstance(data, dict) and '@graph' in data:
                            items = data['@graph']
                        else:
                            items = data if isinstance(data, list) else [data]
                        
                        for item in items:
                            if isinstance(item, dict) and item.get('@type') == 'Product':
                                logger.info(f"    🎯 Found Product in JSON-LD via requests!")
                                offers = item.get('offers', {})
                                if isinstance(offers, dict) and 'price' in offers:
                                    try:
                                        price = float(offers['price'])
                                        logger.info(f"    💰 Extracted price: {price}")
                                        if 0.01 <= price <= 1000:
                                            logger.info(f"✅ Tesco price found via requests fallback: €{price}")
                                            return price
                                    except (ValueError, TypeError) as e:
                                        logger.warning(f"    ⚠️ Price conversion error: {e}")
                                        continue
                    except Exception as pattern_e:
                        logger.warning(f"    ⚠️ JSON pattern {i+1} error: {pattern_e}")
                        continue
                
                # If JSON-LD fails, try regex patterns
                price_patterns = [
                    r'"price"[:\s]*"?(\d+[.,]\d{2})"?',
                    r'€\s*(\d+[.,]\d{2})',
                    r'(\d+[.,]\d{2})\s*€',
                    r'"amount"[:\s]*"?(\d+[.,]\d{2})"?',
                    r'price["\s:]*(\d+[.,]\d{2})',
                ]
                
                for pattern in price_patterns:
                    matches = re.findall(pattern, html_content, re.IGNORECASE)
                    logger.info(f"  🔍 Pattern '{pattern[:30]}...': {len(matches)} matches")
                    for match in matches[:5]:
                        try:
                            price = float(match.replace(',', '.'))
                            if 0.01 <= price <= 1000:
                                logger.info(f"✅ Tesco price found via requests regex: €{price}")
                                return price
                        except ValueError:
                            continue
                
                logger.warning("⚠️ No valid prices found in requests fallback")
                return None
            
            else:
                logger.warning(f"⚠️ Requests fallback failed: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Requests fallback error: {e}")
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
    
    def scrape_dunnes(self, url: str, product_name: str) -> Optional[float]:
        """Scrape Dunnes product - optimized for speed"""
        try:
            logger.info(f"🛒 Scraping Dunnes: {product_name}")
            
            self.driver.get(url)
            time.sleep(5)  # Initial wait for page load
            
            # Check if we got blocked
            page_title = self.driver.title
            logger.info(f"Page title: {page_title}")
            
            # Handle Cloudflare challenge more aggressively
            if "Just a moment" in page_title or "Checking your browser" in self.driver.page_source:
                logger.warning("⚠️ Cloudflare challenge detected, attempting to bypass...")
                
                # Wait for mobile challenge to resolve (shorter timeout)
                max_wait = 30  # Maximum 30 seconds for mobile
                wait_time = 0
                
                while wait_time < max_wait:
                    time.sleep(5)
                    wait_time += 5
                    
                    # Check if challenge resolved
                    current_title = self.driver.title
                    if "Just a moment" not in current_title and "Dunnes Stores" in current_title:
                        logger.info(f"✅ Cloudflare challenge resolved after {wait_time}s")
                        page_title = current_title
                        break
                    
                    logger.info(f"Still waiting for Cloudflare... ({wait_time}s/{max_wait}s)")
                
                # If still blocked after max wait, try alternative approach
                if "Just a moment" in self.driver.title:
                    logger.warning("⚠️ Cloudflare challenge persisting, trying alternative approach...")
                    # Try direct requests approach as fallback
                    return self._scrape_dunnes_requests_fallback(url, product_name)
            
            # Quick check if we're on the product page
            if "Dunnes Stores" in page_title:
                logger.info("✅ Product page loaded successfully")
            
            # First, try to get price from page source (fastest method)
            page_source = self.driver.page_source
            price_patterns = [
                r'"price"[:\s]*"?(\d+[.,]\d{2})"?',
                r'€\s*(\d+[.,]\d{2})',
                r'EUR\s*(\d+[.,]\d{2})',
                r'"amount"[:\s]*"?(\d+[.,]\d{2})"?',
                r'"Price"[:\s]*"?€?(\d+[.,]\d{2})"?'
            ]
            
            for pattern in price_patterns:
                matches = re.findall(pattern, page_source)
                for match in matches:
                    try:
                        price = float(match.replace(',', '.'))
                        if 0.01 <= price <= 1000:
                            logger.info(f"✅ Dunnes price found quickly via regex: €{price}")
                            return price
                    except ValueError:
                        continue
            
            # If regex didn't work, try a few key selectors (but don't waste time)
            key_selectors = [
                '[data-testid*="price"]',
                '.product-price',
                'span.price',
                '[class*="ProductPrice"]'
            ]
            
            for selector in key_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        for element in elements[:3]:  # Only check first 3 elements
                            text = element.text
                            if text and ('€' in text or any(char.isdigit() for char in text)):
                                price = self.extract_price_from_text(text)
                                if price:
                                    logger.info(f"✅ Dunnes price via selector '{selector}': €{price}")
                                    return price
                except:
                    continue
            
            # Last resort: Check for JSON-LD structured data
            try:
                scripts = self.driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for script in scripts[:2]:  # Only check first 2 scripts
                    try:
                        data = json.loads(script.get_attribute('innerHTML'))
                        if isinstance(data, dict) and data.get('@type') == 'Product':
                            offers = data.get('offers', {})
                            if isinstance(offers, dict) and offers.get('price'):
                                price = float(offers['price'])
                                if 0.01 <= price <= 1000:
                                    logger.info(f"✅ Dunnes price via JSON-LD: €{price}")
                                    return price
                    except:
                        continue
            except:
                pass
            
            logger.warning(f"⚠️ Could not find Dunnes price for {product_name}")
            return None
            
        except Exception as e:
            logger.error(f"❌ Dunnes scraping error: {e}")
            return None
    
    def _scrape_dunnes_requests_fallback(self, url: str, product_name: str) -> Optional[float]:
        """Fallback method using requests instead of Selenium for Dunnes"""
        try:
            logger.info("🔄 Trying Dunnes requests fallback method...")
            
            # Mobile headers to mimic mobile browser
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            # Make request with timeout
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                html_content = response.text
                logger.info("✅ Successfully fetched page with requests")
                
                # Try regex patterns on the HTML content
                price_patterns = [
                    r'"price"[:\s]*"?(\d+[.,]\d{2})"?',
                    r'€\s*(\d+[.,]\d{2})',
                    r'EUR\s*(\d+[.,]\d{2})',
                    r'"amount"[:\s]*"?(\d+[.,]\d{2})"?',
                    r'"Price"[:\s]*"?€?(\d+[.,]\d{2})"?',
                    r'price["\s:]*(\d+[.,]\d{2})',
                    r'(\d+[.,]\d{2})\s*€'
                ]
                
                for pattern in price_patterns:
                    matches = re.findall(pattern, html_content, re.IGNORECASE)
                    for match in matches:
                        try:
                            price = float(match.replace(',', '.'))
                            if 0.01 <= price <= 1000:
                                logger.info(f"✅ Dunnes price found via requests fallback: €{price}")
                                return price
                        except ValueError:
                            continue
                
                logger.warning("⚠️ No valid prices found in requests fallback")
                return None
            
            else:
                logger.warning(f"⚠️ Requests fallback failed: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Requests fallback error: {e}")
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
        """Upload price to production API with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
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
                response = self.session.post(f'{API_URL}/api/community-prices/submit', json=data, timeout=30)
                
                if response.status_code in [200, 201]:
                    logger.info(f"✅ Uploaded price for product {alias['product_id']}: €{price}")
                    return True
                elif response.status_code == 429:  # Rate limited
                    logger.warning(f"⚠️ Rate limited, waiting {retry_delay * 2}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(retry_delay * 2)
                    continue
                else:
                    logger.warning(f"⚠️ Upload failed for product {alias['product_id']}: {response.status_code}")
                    if attempt < max_retries - 1:
                        logger.info(f"♾️ Retrying upload in {retry_delay}s... (attempt {attempt + 2}/{max_retries})")
                        time.sleep(retry_delay)
                        continue
                    else:
                        logger.warning(f"Response: {response.text}")
                        return False
                    
            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Upload timeout for product {alias['product_id']} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return False
            except Exception as e:
                logger.error(f"❌ Upload error for product {alias['product_id']} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return False
        
        return False
    
    def scrape_store(self, store_name: str, max_products: int = 5):
        """Scrape all products for a specific store"""
        import random  # Import here for delays
        
        logger.info(f"\n{'='*50}")
        logger.info(f"🏪 Starting {store_name} scraping (max {max_products} products)")
        logger.info(f"{'='*50}")
        
        # Get product aliases for this store
        aliases = self.get_product_aliases(store_name, max_products)
        if not aliases:
            logger.error(f"No aliases found for {store_name}")
            return
        
        results = []
        
        # For Dunnes, use fresh browser session per product to avoid Cloudflare fingerprinting
        use_fresh_session = store_name.lower() == 'dunnes'
        
        for i, alias in enumerate(aliases, 1):
            logger.info(f"\n[{i}/{len(aliases)}] Processing: {alias.get('alias_name', 'Unknown')}")
            logger.info(f"URL: {alias['scraper_url'][:80]}...")
            
            # For Dunnes, create fresh Chrome session for each product
            if use_fresh_session and i > 1:
                logger.info("🔄 Creating fresh Chrome session for Dunnes (Cloudflare avoidance)")
                if self.driver:
                    self.driver.quit()
                    time.sleep(2)  # Brief pause
                self.driver = self.setup_chrome()
                if not self.driver:
                    logger.error("Failed to create fresh Chrome session")
                    continue
            
            start_time = time.time()
            
            # Scrape based on store
            price = None
            if store_name.lower() == 'aldi':
                price = self.scrape_aldi(alias['scraper_url'], alias['alias_name'])
            elif store_name.lower() == 'tesco':
                price = self.scrape_tesco(alias['scraper_url'], alias['alias_name'])
            elif store_name.lower() == 'supervalu':
                price = self.scrape_supervalu(alias['scraper_url'], alias['alias_name'])
            elif store_name.lower() == 'dunnes':
                price = self.scrape_dunnes(alias['scraper_url'], alias['alias_name'])
            
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
            
            # Adaptive delays based on store requirements
            if store_name.lower() == 'dunnes':
                delay = random.randint(15, 25)  # 15-25 seconds for Dunnes
                logger.info(f"⏱️ Waiting {delay}s before next Dunnes product (Cloudflare avoidance)")
                time.sleep(delay)
            elif store_name.lower() in ['tesco', 'supervalu']:
                delay = random.randint(3, 6)  # 3-6 seconds for heavy JS sites
                logger.info(f"⏱️ Waiting {delay}s before next {store_name} product")
                time.sleep(delay)
            else:
                time.sleep(1)  # Minimal delay for Aldi (fast scraping)
        
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
            # Note: Dunnes disabled in GitHub Actions due to Cloudflare blocking
            # Works locally but fails in CI/CD environment
            stores = ['Aldi', 'Tesco', 'SuperValu', 'Dunnes']
        
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
    parser.add_argument('--store', type=str, help='Store name (Aldi, Tesco, SuperValu, Dunnes)')
    parser.add_argument('--all', action='store_true', help='Scrape all stores')
    parser.add_argument('--products', type=int, default=3, help='Max products per store')
    
    args = parser.parse_args()
    
    # Determine stores
    if args.all:
        stores = ['Aldi', 'Tesco', 'SuperValu', 'Dunnes'] 
    elif args.store:
        stores = [args.store]
    else:
        # Default: test all stores with fewer products
        stores = ['Aldi', 'Tesco', 'SuperValu', 'Dunnes']
        args.products = 2
    
    # Run scraper
    scraper = SimpleLocalScraper()
    scraper.run(stores=stores, max_products=args.products)

if __name__ == '__main__':
    main()