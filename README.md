# 🛒 MasterMarket Price Scraper

**Automated price scraping service for MasterMarket - Irish supermarket price tracking platform.**

[![Daily Scraping](https://github.com/your-username/mastermarket-scraper/actions/workflows/daily-scraping.yml/badge.svg)](https://github.com/your-username/mastermarket-scraper/actions/workflows/daily-scraping.yml)

## 🎯 Overview

This repository contains the automated scraping service that collects prices from Irish supermarkets and updates the MasterMarket database. It runs daily at 2:00 AM UTC using GitHub Actions.

## 🏪 Supported Stores

| Store | Status | Performance | Location Requirement |
|-------|--------|-------------|---------------------|
| **Aldi** | ✅ Active | ~5 seconds/product | Any |
| **Tesco** | ✅ Active | ~120 seconds/product | Any |
| **SuperValu** | ✅ Active | ~120 seconds/product | EU/Ireland |
| **Dunnes** | ⚠️ Blocked | Cloudflare protected | GitHub Actions blocked |

## 📊 Daily Performance

- **Products scraped**: 200+ per day (67 per store)
- **Execution time**: ~2.2 hours (parallel execution)
- **Success rate**: 95%+
- **Schedule**: Daily at 2:00 AM UTC

## 🚀 Quick Setup

### 1. Fork this Repository
Click the "Fork" button at the top right of this page.

### 2. Configure Secrets
Go to your forked repository → Settings → Secrets and variables → Actions

Add these secrets:
- `API_URL`: `https://api.mastermarketapp.com`
- `SCRAPER_USERNAME`: Your MasterMarket admin username
- `SCRAPER_PASSWORD`: Your MasterMarket admin password

### 3. Enable GitHub Actions
- Go to the "Actions" tab in your forked repository
- Click "I understand my workflows, go ahead and enable them"

### 4. Test Manual Run
- Go to Actions → Daily Price Scraping → Run workflow
- Select "Run workflow" to test the setup

## 🔧 Configuration

### Schedule Configuration
The scraper runs automatically at 2:00 AM UTC daily. To change the schedule, edit `.github/workflows/daily-scraping.yml`:

```yaml
schedule:
  - cron: '0 2 * * *'  # 2:00 AM UTC (3:00 AM Irish time)
```

### Product Limits
By default, scrapes 67 products per store (200 total). To adjust:
- Edit the `--products` parameter in the workflow file
- Maximum recommended: 100 products per store

## 🏗️ Architecture

```
GitHub Actions (EU Servers)
    ↓ Selenium Chrome
Irish Supermarket Websites
    ↓ REST API
MasterMarket Backend
    ↓ PostgreSQL
Database Updates
```

## 📁 File Structure

```
mastermarket-scraper/
├── .github/workflows/
│   └── daily-scraping.yml       # GitHub Actions configuration
├── simple_local_to_prod.py      # Main scraping script
├── requirements.txt             # Python dependencies
├── README.md                   # This file
└── .gitignore                  # Git ignore rules
```

## 🔍 Monitoring

### Check Scraping Status
- Go to Actions tab to see daily run results
- Each store runs in parallel for faster execution
- Failed runs will show error details

### Logs
- GitHub Actions provides detailed logs for each run
- Logs show progress, prices found, and upload status
- Failed uploads are clearly marked

## 📈 Usage Statistics

### GitHub Actions Limits (Free Tier)
- **Monthly limit**: 2,000 minutes
- **Used per day**: ~132 minutes (66 min × 2 safety factor)
- **Monthly usage**: ~60% of free limit
- **Remaining**: Perfect for daily scraping with buffer

### Performance Metrics
- **Average runtime**: 2.2 hours per day
- **Products per minute**: ~1.8 products
- **Success rate**: 95%+
- **API calls**: ~800 per day

## ⚙️ Technical Details

### Dependencies
```
selenium==4.15.0
webdriver-manager==4.0.1
requests==2.31.0
beautifulsoup4==4.12.2
fake-useragent==1.4.0
```

### Browser Configuration
- **Chrome**: Headless mode with anti-detection
- **User-Agent**: Rotated for each request
- **Location**: EU servers (GitHub Actions)
- **Stealth**: Configured for Irish website compatibility

### API Integration
- **Authentication**: JWT token-based
- **Endpoint**: `/api/community-prices/submit`
- **Data format**: JSON with price, store, location
- **Error handling**: Retry logic with exponential backoff

## 🛡️ Security & Privacy

### No Sensitive Data
- ✅ No credentials stored in code
- ✅ All secrets managed via GitHub Secrets
- ✅ Public repository for transparency
- ✅ No personal data collected

### Compliance
- Respects website rate limits
- Uses reasonable delays between requests
- No aggressive scraping patterns
- EU GDPR compliant

## 🐛 Troubleshooting

### Common Issues

**"Authentication failed"**
- Check your GitHub Secrets are correctly set
- Verify username/password work on MasterMarket website

**"Chrome driver not found"**
- GitHub Actions auto-installs Chrome
- Local testing may require manual Chrome installation

**"Store website blocked request"**
- This is normal occasionally (anti-bot measures)
- Script includes retry logic
- Check if running from supported location (EU for SuperValu)

**"No products found"**
- API might be temporarily down
- Check MasterMarket API status
- Verify product aliases exist for the store

### Support
Open an issue in this repository with:
- Error message
- Store name
- Approximate time of failure
- Screenshots if helpful

## 📊 Monitoring Dashboard

Want to track scraping performance? The data flows to:
- **Frontend**: https://www.mastermarketapp.com
- **Admin Panel**: Available after login
- **API Docs**: https://api.mastermarketapp.com/docs

## 🤝 Contributing

### Ways to Contribute
1. **Report Issues**: Found a bug? Open an issue
2. **Improve Performance**: Optimize scraping speed
3. **Add Stores**: Help add new Irish supermarkets
4. **Documentation**: Improve setup guides

### Development Setup
```bash
# Clone the repository
git clone https://github.com/your-username/mastermarket-scraper.git
cd mastermarket-scraper

# Install dependencies
pip install -r requirements.txt

# Test locally (requires Chrome)
python simple_local_to_prod.py --store Aldi --products 3
# Or test Dunnes
python simple_local_to_prod.py --store Dunnes --products 3
```

## 📄 License

MIT License - See LICENSE file for details

## 🙏 Acknowledgments

- **MasterMarket Team**: For the awesome price tracking platform
- **GitHub**: For free Actions on public repositories  
- **Selenium Community**: For reliable web automation tools
- **Irish Supermarkets**: For providing public price information

---

**⭐ If this scraper helps you save money on groceries, consider starring the repository!**

## 📞 Contact

- **Issues**: Use GitHub Issues for bug reports
- **Main Project**: [MasterMarket](https://www.mastermarketapp.com)
- **API Documentation**: [API Docs](https://api.mastermarketapp.com/docs)

---
*Last updated: December 2024 | Scraping 200+ products daily | 95%+ success rate*