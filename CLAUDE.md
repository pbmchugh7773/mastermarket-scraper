# CLAUDE.md

## Overview

**MasterMarket Price Scraper** - Automated scraping service for Irish supermarkets (Aldi, Tesco, SuperValu, Dunnes). Feeds prices to MasterMarket via REST API. Runs serverless on GitHub Actions daily at 5:00 AM UTC.

**API**: `https://api.mastermarketapp.com`

## Quick Start

```bash
pip install -r requirements.txt

# Scrape single store
./scrape.sh aldi 10                    # or: python simple_local_to_prod.py --store Aldi --products 10

# Scrape all stores
./scrape.sh all 30                     # or: python simple_local_to_prod.py --all --products 30
```

## Scripts Reference

| Script | Purpose | Example |
|--------|---------|---------|
| `simple_local_to_prod.py` | Main Selenium scraper | `python simple_local_to_prod.py --store Tesco --products 50` |
| `scrape.sh` | Convenience wrapper | `./scrape.sh tesco 50` or `./scrape.sh t 50` |
| `apify_tesco_scraper.py` | Cloud-based Tesco scraper | `python apify_tesco_scraper.py --limit 50` |
| `apify_dunnes_scraper.py` | Cloud-based Dunnes scraper | `python apify_dunnes_scraper.py --limit 50` |
| `run_until_done.sh` | Run until complete | `./run_until_done.sh SuperValu 10` |
| `import_tesco_products.py` | Import from JSON | `python import_tesco_products.py data.json` |
| `import_household_products.py` | Import from Excel | `python import_household_products.py --excel file.xlsx` |
| `install_chrome.sh` | Install Chrome (WSL) | `./install_chrome.sh` |
| `install_chromedriver.py` | Install ChromeDriver | `python install_chromedriver.py` |

### Main Scraper Options

```bash
python simple_local_to_prod.py --store <store> --products <n>  # Basic
python simple_local_to_prod.py --store Tesco --retry-mode      # Only failed/pending
python simple_local_to_prod.py --store Aldi --debug-prices     # Verbose logging
python simple_local_to_prod.py --product-id 4573 --store Tesco # Single product
python simple_local_to_prod.py --all --products 67             # All stores
```

### scrape.sh Shortcuts

| Shortcut | Store |
|----------|-------|
| `aldi`, `a` | Aldi |
| `tesco`, `t` | Tesco |
| `supervalu`, `sv` | SuperValu |
| `dunnes`, `d` | Dunnes Stores |
| `all` | All stores |

Options: `-r` (retry), `-d` (debug), `-p N` (products)

## Store Performance

| Store | Speed | Method |
|-------|-------|--------|
| Aldi | ~2s/product | JSON-LD + CSS selectors |
| Tesco | ~10s/product | Hybrid Selenium/requests |
| SuperValu | ~129s/product | JSON-LD @graph + JS handling |
| Dunnes | ~8s/product | Regex + Cloudflare bypass |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `API_URL` | API endpoint | `https://api.mastermarketapp.com` |
| `SCRAPER_USERNAME` | Auth username | `pricerIE@mastermarket.com` |
| `SCRAPER_PASSWORD` | Auth password | - |
| `APIFY_API_TOKEN` | Apify API (for apify scripts) | - |
| `APIFY_DUNNES_ACTOR_ID` | Custom Dunnes actor ID | `YOUR_USERNAME/dunnes-scraper` |
| `MASTERMARKET_API_URL` | Import scripts API | `http://localhost:8000` |
| `MASTERMARKET_EMAIL` | Import scripts auth | - |
| `MASTERMARKET_PASSWORD` | Import scripts auth | - |

## API Endpoints

```bash
# Auth
POST /auth/login  (username, password) → {access_token}

# Get products to scrape
GET /api/admin/product-aliases?store={store}&limit={n}

# Submit price
POST /api/community-prices/submit
{product_id, price, store, location, date_recorded}
```

## Architecture

```
GitHub Actions (5 AM UTC) → Matrix (4 parallel jobs) → Selenium Chrome
    → Scrape stores → MasterMarket API → PostgreSQL
```

**Core class**: `SimpleLocalScraper` in `simple_local_to_prod.py`
- `authenticate()` - JWT login
- `setup_chrome()` - Headless Chrome with anti-detection
- `get_product_aliases()` - Fetch products from API
- `scrape_[store]()` - Store-specific logic
- `upload_price()` - Submit with retry

## GitHub Actions

**Workflows**:
- `.github/workflows/daily-scraping.yml` - Main Selenium scraper (Aldi, SuperValu)
- `.github/workflows/apify-tesco.yml` - Apify-based Tesco scraper
- `.github/workflows/apify-dunnes.yml` - Apify-based Dunnes scraper

**Secrets required**: `API_URL`, `SCRAPER_USERNAME`, `SCRAPER_PASSWORD`, `APIFY_API_TOKEN`, `APIFY_DUNNES_ACTOR_ID`

```bash
# Manual trigger: Actions → [Workflow Name] → Run workflow
# Apify scrapers run on Tuesdays and Fridays at 6:00 AM UTC (retry at 8:00 AM)
```

## Adding New Store

1. Add `scrape_newstore()` method to `SimpleLocalScraper`
2. Add store to GitHub Actions matrix in `daily-scraping.yml`
3. Create product aliases in MasterMarket database
4. Test: `python simple_local_to_prod.py --store NewStore --products 3`

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Auth failure | Check `SCRAPER_USERNAME`/`SCRAPER_PASSWORD` |
| Chrome not found | Run `./install_chrome.sh` (local) or check Actions setup |
| Store blocking | Normal - retry logic handles it |
| No products | Verify aliases exist in API for store |

## Debugging

```bash
google-chrome --version && chromedriver --version  # Check installation
cat *.log                                          # View logs
curl -X POST https://api.mastermarketapp.com/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=$SCRAPER_USERNAME&password=$SCRAPER_PASSWORD"
```
