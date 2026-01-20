# Dunnes Stores Scraper - Apify Actor

Custom Apify actor for scraping Dunnes Stores Ireland product prices with Cloudflare bypass.

## Features

- Cloudflare bypass using residential proxies
- Price extraction from JSON-LD and HTML
- Promotion detection (multi-buy, percentage off, fixed amount, was/now)
- Session pooling for better anti-bot handling
- Resource blocking for faster scraping

## Output Format

```json
{
    "url": "https://www.dunnesstores.com/c/product/123456",
    "title": "Product Name",
    "price": 2.99,
    "originalPrice": 3.99,
    "promotionType": "temporary_discount",
    "promotionText": "Was €3.99",
    "promotionDiscountValue": 1.00,
    "scrapedAt": "2024-01-20T10:30:00.000Z"
}
```

## Deployment to Apify

### Option 1: Deploy via Apify CLI (Recommended)

1. Install Apify CLI:
   ```bash
   npm install -g apify-cli
   ```

2. Login to Apify:
   ```bash
   apify login
   ```

3. Navigate to this directory:
   ```bash
   cd apify-actors/dunnes-scraper
   ```

4. Create the actor on Apify:
   ```bash
   apify create dunnes-scraper --template=puppeteer-crawler
   ```
   (Select "No" when asked to initialize from template - we have our own code)

5. Push the actor:
   ```bash
   apify push
   ```

6. Note your actor ID (shown after push), e.g., `your-username/dunnes-scraper`

### Option 2: Deploy via Apify Console

1. Go to [Apify Console](https://console.apify.com)

2. Click "Actors" in the sidebar

3. Click "Create new" > "Actor"

4. Name it `dunnes-scraper`

5. In the "Source" tab:
   - Select "Multiple source files"
   - Upload all files from this directory

6. Click "Build" to build the actor

7. Note your actor ID from the URL, e.g., `your-username/dunnes-scraper`

### Option 3: Deploy from GitHub

1. Fork or push this code to your GitHub repository

2. In Apify Console, create a new actor

3. In "Source" tab, select "Git repository"

4. Enter your repository URL and the path to this directory

5. Build the actor

## Configuration

After deploying, set up the GitHub Actions secret:

```
APIFY_DUNNES_ACTOR_ID=your-username/dunnes-scraper
```

## Input Schema

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| urls | array | required | List of Dunnes product URLs |
| maxConcurrency | integer | 5 | Max parallel requests |
| maxRequestRetries | integer | 3 | Retries per failed request |
| requestHandlerTimeoutSecs | integer | 120 | Timeout per page |
| useResidentialProxies | boolean | true | Use residential proxies |
| proxyCountryCode | string | "IE" | Proxy country code |

## Testing the Actor

1. In Apify Console, go to your actor

2. Click "Run" tab

3. Enter test input:
   ```json
   {
       "urls": [
           "https://www.dunnesstores.com/c/cadbury-dairy-milk-bar/317632017dpie"
       ],
       "maxConcurrency": 1,
       "useResidentialProxies": true
   }
   ```

4. Click "Start"

5. Check the "Dataset" tab for results

## Local Testing

```bash
cd apify-actors/dunnes-scraper
npm install
apify run --input='{"urls": ["https://www.dunnesstores.com/c/product/123"]}'
```

## Proxy Configuration

For Cloudflare bypass, residential proxies are recommended. Apify provides these through:

- **RESIDENTIAL** proxy group - Best for Cloudflare
- **SHADER** proxy group - Good alternative

The actor defaults to RESIDENTIAL proxies with Irish IPs.

## Costs

Estimated costs per run:
- Compute: ~$0.01-0.02 per 100 products
- Residential proxies: ~$0.50-1.00 per 100 products
- Total: ~$0.60-1.20 per 100 products

Monthly estimate (200 products, 8 runs): ~$5-10

## Troubleshooting

### Cloudflare blocks persist
- Try increasing `requestHandlerTimeoutSecs` to 180
- Reduce `maxConcurrency` to 2-3
- Ensure residential proxies are enabled

### No prices extracted
- Check if Dunnes changed their HTML structure
- Review actor logs for specific errors
- Try with a single URL first

### High costs
- Reduce `maxConcurrency` to lower proxy usage
- Run less frequently
- Use `maxRequestRetries: 1` for less critical runs
