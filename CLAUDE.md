# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is the **MasterMarket Price Scraper** - a standalone, automated scraping service that collects prices from Irish supermarkets and feeds them to the main MasterMarket platform via REST API. It operates independently using GitHub Actions for serverless execution.

**Key Architecture Points:**
- **Microservice Design**: Completely separate from main MasterMarket repository
- **GitHub Actions Based**: Runs on GitHub's infrastructure (no server required)
- **API Client**: Communicates with MasterMarket backend at `https://api.mastermarketapp.com`
- **Daily Schedule**: Executes at 2:00 AM UTC (3:00 AM Irish time)

## Development Commands

### Local Testing
```bash
# Install dependencies
pip install -r requirements.txt

# Test single store with limited products
python simple_local_to_prod.py --store Aldi --products 3

# Test all stores
python simple_local_to_prod.py --store Aldi --products 67
python simple_local_to_prod.py --store Tesco --products 67
python simple_local_to_prod.py --store SuperValu --products 67
```

### GitHub Actions Testing
```bash
# Manual workflow trigger (from GitHub UI)
Actions → Daily Price Scraping → Run workflow → Set max_products → Run

# Monitor execution
Actions tab → Click running workflow → View job progress
```

### Debugging Commands
```bash
# Check Chrome/ChromeDriver installation (local)
google-chrome --version
chromedriver --version

# View scraping logs
cat *.log

# Test API authentication
curl -X POST https://api.mastermarketapp.com/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=YOUR_USERNAME&password=YOUR_PASSWORD"
```

## High-Level Architecture

### Data Flow
```
GitHub Actions Scheduler (2 AM UTC)
    ↓ Triggers workflow
Matrix Strategy (3 parallel jobs)
    ↓ One job per store
Selenium Chrome (Headless)
    ↓ Scrapes websites
Store Websites (Aldi/Tesco/SuperValu)
    ↓ Extracts prices
MasterMarket API
    ↓ JWT authentication
Product Aliases Endpoint
    ↓ Gets products to scrape
Price Submission Endpoint
    ↓ Uploads scraped prices
PostgreSQL Database
    ↓ Stores price history
MasterMarket Platform
```

### Core Components

**`simple_local_to_prod.py`** - Main scraper class:
- `authenticate()`: JWT token-based API login
- `setup_chrome()`: Configures headless Chrome with anti-detection
- `get_product_aliases()`: Fetches products to scrape from API
- `scrape_[store]()`: Store-specific scraping logic (Aldi, Tesco, SuperValu, Dunnes)
- `upload_price()`: Submits prices to API with retry logic

**GitHub Actions Workflow** (`daily-scraping.yml`):
- **Matrix Strategy**: Parallel execution for 4 stores
- **Environment Setup**: Chrome, ChromeDriver, Python dependencies
- **Secret Management**: API credentials via GitHub Secrets
- **Artifact Storage**: Logs retained for 7 days

### Store-Specific Considerations

| Store | Scraping Method | Performance | Special Requirements |
|-------|----------------|-------------|---------------------|
| **Aldi** | CSS selectors, simple HTML | ~5 sec/product | None |
| **Tesco** | JavaScript-heavy, dynamic loading | ~120 sec/product | Extended timeouts |
| **SuperValu** | JSON-LD structured data | ~120 sec/product | EU location (GitHub Actions EU servers) |
| **Dunnes** | Regex-based extraction, optimized for speed | ~15 sec/product | None |

## Critical Configuration

### Required GitHub Secrets
- `API_URL`: Production API endpoint (`https://api.mastermarketapp.com`)
- `SCRAPER_USERNAME`: MasterMarket admin username
- `SCRAPER_PASSWORD`: MasterMarket admin password

### Chrome Configuration
- **Headless Mode**: Required for GitHub Actions
- **User Agent Rotation**: Anti-detection via fake-useragent
- **Stealth Settings**: Disable automation flags
- **Virtual Display**: Xvfb for GUI-requiring operations

### Performance Limits
- **Default**: 67 products per store (268 total daily)
- **Maximum Recommended**: 100 products per store
- **GitHub Actions Limit**: 2,000 minutes/month (uses ~65%)
- **Execution Time**: ~2.2 hours total (parallel)

## API Integration Points

### Authentication
```python
POST /auth/login
Body: username={username}&password={password}
Returns: { "access_token": "jwt_token" }
```

### Product Aliases
```python
GET /api/admin/product-aliases?store={store}&limit={limit}
Headers: Authorization: Bearer {token}
Returns: List of products with URLs to scrape
```

### Price Submission
```python
POST /api/community-prices/submit
Headers: Authorization: Bearer {token}
Body: {
    "product_id": int,
    "price": float,
    "store": str,
    "location": str,
    "date_recorded": str
}
```

## Error Handling Patterns

### Retry Logic
- **API Calls**: 3 retries with exponential backoff
- **Page Loading**: Store-specific timeouts (Aldi: 10s, Others: 30s)
- **Element Finding**: Multiple selector strategies
- **Price Extraction**: Fallback patterns for different formats

### Common Issues
- **Authentication Failures**: Check GitHub Secrets configuration
- **Chrome Not Found**: GitHub Actions auto-installs, verify locally
- **Store Blocking**: Normal anti-bot behavior, includes retry logic
- **No Products Found**: Verify API has product aliases for store

## Monitoring & Maintenance

### Success Metrics
- **Daily Success Rate**: Target 95%+
- **Products Scraped**: ~268 per day
- **Execution Time**: <3 hours
- **API Upload Rate**: ~99% successful uploads

### Health Checks
- GitHub Actions dashboard for execution history
- Artifact logs for detailed debugging
- MasterMarket admin panel for price updates
- API endpoint monitoring at `/health/`

## Development Workflow

### Adding New Store
1. Add scraping method to `SimpleLocalScraper` class
2. Follow naming pattern: `scrape_[storename]()`
3. Add store to GitHub Actions matrix
4. Create product aliases in MasterMarket database
5. Test locally with small product count
6. Deploy and monitor first production run

### Modifying Schedule
Edit `.github/workflows/daily-scraping.yml`:
```yaml
schedule:
  - cron: '0 2 * * *'  # Modify this line
```

### Performance Optimization
- Reduce products per store in workflow inputs
- Optimize selectors for faster element finding
- Implement caching for static elements
- Consider browser session reuse for same-store products