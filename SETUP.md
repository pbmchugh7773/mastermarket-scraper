# 🚀 Setup Guide - MasterMarket Scraper

Complete setup instructions to get the scraper running on GitHub Actions.

## 📋 Prerequisites

- GitHub account
- MasterMarket admin account (for API access)
- Basic understanding of GitHub Actions (optional)

## 🔧 Step-by-Step Setup

### 1. Fork the Repository

1. Go to the main repository page
2. Click the **Fork** button (top right)
3. Select your GitHub account
4. Wait for the fork to complete

### 2. Configure Repository Secrets

GitHub Secrets keep your credentials safe. Never put passwords directly in code!

1. Go to your forked repository
2. Click **Settings** tab
3. Click **Secrets and variables** → **Actions**
4. Click **New repository secret** for each of these:

#### Required Secrets:

| Secret Name | Value | Description |
|-------------|-------|-------------|
| `API_URL` | `https://api.mastermarketapp.com` | MasterMarket API endpoint |
| `SCRAPER_USERNAME` | Your email | Admin username for MasterMarket |
| `SCRAPER_PASSWORD` | Your password | Admin password for MasterMarket |

#### How to Add Each Secret:
1. Click **New repository secret**
2. Enter the **Name** (exactly as shown above)
3. Enter the **Secret** (the actual value)
4. Click **Add secret**

### 3. Enable GitHub Actions

1. Go to the **Actions** tab in your repository
2. If you see a message about workflows, click **"I understand my workflows, go ahead and enable them"**
3. You should now see the workflow: "Daily Price Scraping"

### 4. Test the Setup

Before waiting for the nightly run, test it manually:

1. Go to **Actions** tab
2. Click **Daily Price Scraping** workflow
3. Click **Run workflow** button (right side)
4. Leave "Max products per store" as default (67)
5. Click **Run workflow** (green button)

### 5. Monitor the Test Run

1. Click on the running workflow to see progress
2. You'll see 4 jobs running in parallel:
   - Scrape Aldi
   - Scrape Tesco  
   - Scrape SuperValu
   - Scrape Dunnes
3. Each job should complete successfully (✅)

Expected timeline:
- **Aldi**: ~6 minutes
- **Tesco**: ~2.2 hours  
- **SuperValu**: ~2.2 hours
- **Dunnes**: ~15 minutes
- **Total**: ~2.2 hours (parallel execution)

## 📊 Verify Results

After the scraper completes:

1. Go to [MasterMarket Products](https://www.mastermarketapp.com/products)
2. Search for products from the scraped stores
3. Check that prices were updated today
4. Look for the "recent prices" section on product pages

## 📅 Automatic Schedule

Once set up, the scraper runs automatically:
- **Time**: 2:00 AM UTC daily (3:00 AM Irish time)
- **Products**: ~268 per day (67 per store)
- **Duration**: ~2.2 hours
- **Success Rate**: 95%+

## 🔍 Monitoring & Maintenance

### Check Daily Results
1. Go to **Actions** tab in your repository
2. Look for daily "Daily Price Scraping" runs
3. Green checkmark = success, Red X = failure

### Common Success Indicators
- All 4 store jobs complete with ✅
- Logs show "Uploaded price for product X"
- No authentication errors
- Runtime ~2.2-3 hours total

### If Something Fails
1. Click on the failed workflow
2. Click on the failed job (red X)
3. Expand the failed step to see error details
4. See [Troubleshooting](#troubleshooting) section below

## 🐛 Troubleshooting

### Authentication Errors
```
❌ Authentication failed: 401
```
**Solution**: 
- Verify your GitHub Secrets are set correctly
- Test your username/password on https://www.mastermarketapp.com
- Make sure you're using an admin account

### Chrome/Selenium Errors
```
❌ Chrome driver not found
```
**Solution**:
- This is rare on GitHub Actions (Chrome is pre-installed)
- Try re-running the workflow
- Check if there are Chrome updates affecting the runner

### Store Website Blocking
```
⚠️ Could not find price for product X
```
**Solution**:
- This is normal occasionally (anti-bot measures)
- Script includes retry logic
- SuperValu requires EU location (GitHub Actions EU servers work)
- If consistently failing, check store website changes

### API Errors
```
❌ Upload failed for product X: 500
```
**Solution**:
- Check MasterMarket API status
- Verify your admin account has permission to submit prices
- Try manual re-run after a few minutes

### No Products Found
```
No aliases found for [Store]
```
**Solution**:
- Check that product aliases exist in the MasterMarket database
- Verify the store name matches exactly (Aldi, Tesco, SuperValu, Dunnes)
- Contact MasterMarket admin if aliases are missing

## 📊 Performance Optimization

### Reducing Runtime
If scraping takes too long, you can reduce products per store:

1. Go to **Actions** → **Daily Price Scraping**
2. Click **Run workflow**
3. Change "Max products per store" from 67 to a lower number (e.g., 50)
4. This reduces total products but completes faster

### Changing Schedule
To change the 2 AM daily schedule:

1. Edit `.github/workflows/daily-scraping.yml`
2. Modify the cron expression:
   ```yaml
   schedule:
     - cron: '0 2 * * *'  # Hour Minute * * *
   ```
3. Use [Crontab Guru](https://crontab.guru) to generate new times

## 🔒 Security Best Practices

### ✅ Good Practices
- All credentials stored as GitHub Secrets
- Repository can be public (no sensitive data)
- Secrets are encrypted and hidden from logs
- Regular monitoring of access logs

### ❌ Never Do This
- Don't put passwords in code files
- Don't commit API keys to the repository  
- Don't share your GitHub Secrets with others
- Don't use personal accounts for production

## 📈 Scaling Up

### More Products Per Day
- Current: 268 products/day (67 per store)
- Max recommended: 400 products/day (100 per store)
- Constraint: GitHub Actions 2,000 minutes/month limit

### Additional Stores
To add new Irish supermarkets:
1. Add store logic to `simple_local_to_prod.py`
2. Add store to workflow matrix
3. Create product aliases in MasterMarket database
4. Test thoroughly before production

### Multiple Runs Per Day
Currently runs once daily. To run twice:
```yaml
schedule:
  - cron: '0 2 * * *'  # 2 AM
  - cron: '0 14 * * *'  # 2 PM
```

## 📞 Support

### Getting Help
1. **GitHub Issues**: Best for bugs and feature requests
2. **Workflow Logs**: Always check these first
3. **MasterMarket Support**: For API-related issues
4. **Community**: Other users may have similar issues

### Useful Resources
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Selenium Documentation](https://selenium-python.readthedocs.io/)
- [MasterMarket API Docs](https://api.mastermarketapp.com/docs)
- [Cron Schedule Helper](https://crontab.guru)

## 🎉 Success!

If you've completed this setup:
- ✅ Your scraper runs automatically every night
- ✅ 200+ products get updated daily
- ✅ MasterMarket has fresh price data
- ✅ You're helping Irish shoppers save money!

---

**Questions?** Open an issue in the repository and we'll help you get it working!