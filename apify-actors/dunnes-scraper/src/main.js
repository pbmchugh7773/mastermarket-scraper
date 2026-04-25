/**
 * Dunnes Stores Ireland Price Scraper
 *
 * Apify actor using Crawlee with Puppeteer for scraping Dunnes Stores
 * product prices. Uses residential proxies to bypass Cloudflare protection.
 *
 * Output format:
 * {
 *   url: string,
 *   title: string,
 *   price: number,
 *   originalPrice: number | null,
 *   promotionType: string | null,
 *   promotionText: string | null,
 *   promotionDiscountValue: number | null,
 *   scrapedAt: string (ISO date)
 * }
 */

import { Actor } from 'apify';
import { PuppeteerCrawler, Dataset } from 'crawlee';

// Price extraction patterns
const PRICE_PATTERNS = [
    /"price"[:\s]*"?(\d+[.,]\d{2})"?/i,
    /€\s*(\d+[.,]\d{2})/,
    /EUR\s*(\d+[.,]\d{2})/i,
    /"amount"[:\s]*"?(\d+[.,]\d{2})"?/i,
];

// Multi-buy promotion patterns.
// MASA-115: require explicit `.\d{2}` cents — bare `(\d+) for (\d+)` was
// matching arbitrary body copy ("3 for 2 portions of cereal"). Bundle math
// is also validated against the current price before tagging.
const MULTIBUY_PATTERNS = [
    { pattern: /buy\s*(\d+)\s*for\s*€?\s*(\d+[.,]\d{2})\b/i, type: 'buy_x_for' },
    { pattern: /(?:mix\s*&?\s*match\s+)?any\s*(\d+)\s*for\s*€?\s*(\d+[.,]\d{2})\b/i, type: 'any_x_for' },
    { pattern: /buy\s*(\d+)\s*get\s*(\d+)\s*free\b/i, type: 'bogo' },
];

// Percentage discount patterns
const PERCENTAGE_PATTERNS = [
    /(\d+)\s*%\s*off/i,
    /save\s*(\d+)\s*%/i,
    /half\s*price/i,
];

// Fixed amount discount patterns
const SAVINGS_PATTERNS = [
    /save\s*€?\s*(\d+[.,]\d{2})/i,
    /€?\s*(\d+[.,]\d{2})\s*off/i,
];

// Was/Now price patterns. `\b` after `<s` / `<del` is critical — without
// it, `<s[^>]*>` greedily matches `<span ...>` and we'd parse the current
// price as the original.
const WAS_PATTERNS = [
    /was\s*€?\s*(\d+[.,]\d{2})/i,
    /original\s*price[:\s]*€?\s*(\d+[.,]\d{2})/i,
    /<s\b[^>]*>\s*€?\s*(\d+[.,]\d{2})/i,
    /<del\b[^>]*>\s*€?\s*(\d+[.,]\d{2})/i,
];

/**
 * Extract price from text using multiple patterns
 */
function extractPrice(text) {
    for (const pattern of PRICE_PATTERNS) {
        const matches = text.match(new RegExp(pattern, 'g'));
        if (matches) {
            for (const match of matches) {
                const priceMatch = match.match(pattern);
                if (priceMatch) {
                    const price = parseFloat(priceMatch[1].replace(',', '.'));
                    // Reject €1.00 placeholder: Dunnes returns €1 when a product
                    // can't be parsed properly. Real items at exactly €1.00 are
                    // rare enough that the false-positive risk is acceptable.
                    if (price === 1) continue;
                    if (price >= 0.01 && price <= 1000) {
                        return price;
                    }
                }
            }
        }
    }
    return null;
}

/**
 * Sanity-check a detected multi-buy (qty, total) against the product's
 * current unit price. Per-unit must be ≤ current_price (5% rounding
 * tolerance) and ≥ 50% (retail grocery deals rarely exceed -50%). If
 * currentPrice is unknown, accept.
 */
function isPlausibleMultibuy(qty, total, currentPrice) {
    if (currentPrice == null) return true;
    if (!currentPrice || qty < 1 || total <= 0) return false;
    const perUnit = total / qty;
    return currentPrice * 0.5 <= perUnit && perUnit <= currentPrice * 1.05;
}

/**
 * Search HTML for evidence of a real original/was price. Used to gate
 * percentage_off / fixed_amount_off — Dunnes renders decorative "Save €X"
 * / "25% Off" badges on non-discounted PDPs (MASA-113), so the badge text
 * alone is not sufficient evidence of a real promotion.
 *
 * Returns the parsed original price ONLY if strictly greater than
 * currentPrice. Otherwise returns null.
 */
function findOriginalPrice(htmlLower, currentPrice) {
    if (!currentPrice) return null;
    for (const pattern of WAS_PATTERNS) {
        const re = new RegExp(pattern.source, 'gi');
        let m;
        while ((m = re.exec(htmlLower)) !== null) {
            const candidate = parseFloat(m[1].replace(',', '.'));
            if (Number.isFinite(candidate) && candidate > currentPrice) {
                return Math.round(candidate * 100) / 100;
            }
        }
    }
    return null;
}

/**
 * Detect promotion data from page content
 */
function detectPromotion(html, currentPrice) {
    const result = {
        promotionType: null,
        promotionText: null,
        promotionDiscountValue: null,
        originalPrice: null,
    };

    const htmlLower = html.toLowerCase();

    // 1. Multi-buy (highest priority for Dunnes). Strict mechanic regex +
    //    bundle-math validation (MASA-115). Pre-fix the loose `(\d+) for
    //    (\d+)` pattern matched random body copy.
    for (const { pattern, type } of MULTIBUY_PATTERNS) {
        const match = htmlLower.match(pattern);
        if (!match) continue;

        if (type === 'buy_x_for' || type === 'any_x_for') {
            const qty = parseInt(match[1], 10);
            const price = parseFloat(match[2].replace(',', '.'));
            if (!isPlausibleMultibuy(qty, price, currentPrice)) {
                continue;
            }
            const prefix = type === 'any_x_for' ? 'Any' : 'Buy';
            result.promotionType = 'multi_buy';
            result.promotionText = `${prefix} ${qty} for €${price.toFixed(2)}`;
            return result;
        } else if (type === 'bogo') {
            result.promotionType = 'multi_buy';
            result.promotionText = `Buy ${match[1]} Get ${match[2]} Free`;
            return result;
        }
    }

    // 2. Was/Now evidence — required to gate percentage_off / fixed_amount_off.
    //    MASA-113 audit: 1,680 fixed_amount_off + 417 percentage_off historical
    //    rows tagged on non-discounted Dunnes products since 2026-01.
    const originalPrice = findOriginalPrice(htmlLower, currentPrice);

    // 3. Percentage discounts — only if was/now evidence is present.
    for (const pattern of PERCENTAGE_PATTERNS) {
        const match = htmlLower.match(pattern);
        if (!match) continue;
        if (originalPrice == null) {
            // Decorative badge, drop bucket entirely.
            break;
        }
        if (pattern.source.includes('half')) {
            result.promotionType = 'percentage_off';
            result.promotionText = 'Half Price';
            result.originalPrice = originalPrice;
            result.promotionDiscountValue = Math.round((originalPrice - currentPrice) * 100) / 100;
            return result;
        }
        const discount = parseFloat(match[1]);
        if (discount > 0 && discount <= 90) {
            result.promotionType = 'percentage_off';
            result.promotionText = `${Math.round(discount)}% Off`;
            result.originalPrice = originalPrice;
            result.promotionDiscountValue = Math.round((originalPrice - currentPrice) * 100) / 100;
            return result;
        }
    }

    // 4. Fixed amount savings — only if was/now evidence is present.
    for (const pattern of SAVINGS_PATTERNS) {
        const match = htmlLower.match(pattern);
        if (!match) continue;
        if (originalPrice == null) {
            break;
        }
        const amount = parseFloat(match[1].replace(',', '.'));
        if (amount > 0 && amount < 100) {
            result.promotionType = 'fixed_amount_off';
            result.promotionText = `Save €${amount.toFixed(2)}`;
            result.originalPrice = originalPrice;
            result.promotionDiscountValue = Math.round((originalPrice - currentPrice) * 100) / 100;
            return result;
        }
    }

    // 5. Standalone was/now (no badge text) — surface as a temporary_discount
    //    so downstream callers still get an originalPrice when a strikethrough
    //    is present without an explicit "Save €X" badge.
    if (originalPrice != null) {
        result.originalPrice = originalPrice;
        result.promotionType = 'temporary_discount';
        result.promotionText = `Was €${originalPrice.toFixed(2)}`;
        result.promotionDiscountValue = Math.round((originalPrice - currentPrice) * 100) / 100;
        return result;
    }

    return result;
}

/**
 * Extract product data from JSON-LD structured data
 */
function extractFromJsonLd(scripts) {
    for (const script of scripts) {
        try {
            const data = JSON.parse(script);
            if (data['@type'] === 'Product') {
                const offers = data.offers || {};
                const price = parseFloat(offers.price);
                // Reject €1.00 placeholder (see extractPrice).
                if (price === 1) continue;
                if (price >= 0.01 && price <= 1000) {
                    return {
                        title: data.name || '',
                        price: price,
                    };
                }
            }
        } catch (e) {
            // Skip invalid JSON
        }
    }
    return null;
}

// Main actor logic
await Actor.init();

const input = await Actor.getInput();

const {
    urls = [],
    maxConcurrency = 5,
    maxRequestRetries = 3,
    requestHandlerTimeoutSecs = 120,
    useResidentialProxies = true,
    proxyCountryCode = 'IE',
} = input;

if (!urls || urls.length === 0) {
    console.log('No URLs provided. Exiting.');
    await Actor.exit();
}

console.log(`Starting Dunnes Stores scraper with ${urls.length} URLs`);
console.log(`Max concurrency: ${maxConcurrency}`);
console.log(`Use residential proxies: ${useResidentialProxies}`);

// Configure proxy based on input setting
let proxyConfiguration = null;
if (useResidentialProxies) {
    // Residential proxy - more expensive but bypasses Cloudflare better
    proxyConfiguration = await Actor.createProxyConfiguration({
        groups: ['RESIDENTIAL'],
        countryCode: proxyCountryCode,
    });
    console.log(`Proxy configured: RESIDENTIAL for country: ${proxyCountryCode}`);
} else {
    // Datacenter proxy - cheaper, may get blocked by Cloudflare
    // Note: Free tier datacenter doesn't have IE proxies, so we don't specify country
    proxyConfiguration = await Actor.createProxyConfiguration({
        // No countryCode - use any available datacenter proxy
    });
    console.log(`Proxy configured: DATACENTER (default, any country)`);
}

// Create the crawler
const crawler = new PuppeteerCrawler({
    proxyConfiguration,
    maxConcurrency,
    maxRequestRetries,
    requestHandlerTimeoutSecs,

    // Use session pool for better anti-bot handling
    useSessionPool: true,
    sessionPoolOptions: {
        maxPoolSize: 20,
        sessionOptions: {
            maxAgeSecs: 3600,
            maxUsageCount: 50,
        },
    },

    // Browser launch options
    launchContext: {
        launchOptions: {
            headless: true,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu',
                '--window-size=1920,1080',
            ],
        },
    },

    // Pre-navigation hooks
    preNavigationHooks: [
        async ({ page }, gotoOptions) => {
            // Return control once DOM is ready — avoids waiting for late analytics XHRs
            gotoOptions.waitUntil = 'domcontentloaded';

            // Set realistic viewport
            await page.setViewport({ width: 1920, height: 1080 });

            // Set user agent
            await page.setUserAgent(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            );

            // Block unnecessary resources to reduce bandwidth (saves proxy costs)
            await page.setRequestInterception(true);

            // Domains to block (tracking, analytics, ads)
            const blockedDomains = [
                'google-analytics.com',
                'googletagmanager.com',
                'facebook.net',
                'facebook.com',
                'doubleclick.net',
                'googlesyndication.com',
                'hotjar.com',
                'newrelic.com',
                'nr-data.net',
                'segment.io',
                'segment.com',
                'optimizely.com',
                'crazyegg.com',
                'fullstory.com',
                'mouseflow.com',
                'clarity.ms',
                'bing.com',
                'twitter.com',
                'linkedin.com',
                'pinterest.com',
                'tiktok.com',
                'snapchat.com',
            ];

            page.on('request', (request) => {
                const resourceType = request.resourceType();
                const url = request.url();

                // Block by resource type
                if (['image', 'stylesheet', 'font', 'media', 'texttrack', 'eventsource', 'websocket'].includes(resourceType)) {
                    request.abort();
                    return;
                }

                // Block tracking/analytics domains
                if (blockedDomains.some(domain => url.includes(domain))) {
                    request.abort();
                    return;
                }

                // Block common tracking patterns in URLs
                if (url.includes('/analytics') ||
                    url.includes('/tracking') ||
                    url.includes('/pixel') ||
                    url.includes('gtm.js') ||
                    url.includes('gtag/js')) {
                    request.abort();
                    return;
                }

                request.continue();
            });
        },
    ],

    // Main request handler
    async requestHandler({ page, request, log }) {
        const url = request.url;
        log.info(`Processing: ${url}`);

        // Brief wait for initial page load (reduced from 3s to save bandwidth)
        await page.waitForTimeout(1500);

        // Check for Cloudflare challenge
        const title = await page.title();
        if (title.includes('Just a moment') || title.includes('Checking your browser')) {
            log.warning('Cloudflare challenge detected, waiting...');

            // Wait for challenge to resolve (up to 30 seconds)
            let attempts = 0;
            while (attempts < 6) {
                await page.waitForTimeout(5000);
                attempts++;

                const currentTitle = await page.title();
                if (!currentTitle.includes('Just a moment') &&
                    (currentTitle.toLowerCase().includes('dunnes') || currentTitle.length > 20)) {
                    log.info(`Cloudflare resolved after ${attempts * 5}s`);
                    break;
                }
                log.info(`Still waiting for Cloudflare... (${attempts * 5}s)`);
            }

            // Final check
            const finalTitle = await page.title();
            if (finalTitle.includes('Just a moment')) {
                log.error('Cloudflare challenge not resolved');
                return; // Skip this URL
            }
        }

        // Get page content
        const html = await page.content();

        // Try to extract from JSON-LD first
        const jsonLdScripts = await page.$$eval(
            'script[type="application/ld+json"]',
            (scripts) => scripts.map((s) => s.textContent)
        );

        let productData = extractFromJsonLd(jsonLdScripts);
        let price = productData?.price;
        let productTitle = productData?.title || '';

        // Fallback: extract price from HTML
        if (!price) {
            price = extractPrice(html);
        }

        // Try to get title from page if not from JSON-LD
        if (!productTitle) {
            try {
                productTitle = await page.$eval('h1', (el) => el.textContent.trim());
            } catch (e) {
                // Try meta title
                try {
                    productTitle = await page.$eval('meta[property="og:title"]', (el) => el.content);
                } catch (e2) {
                    productTitle = title.replace(' | Dunnes Stores', '').trim();
                }
            }
        }

        if (!price) {
            log.warning(`No price found for ${url}`);
            return;
        }

        // Detect promotions
        const promotionData = detectPromotion(html, price);

        // Build result object
        const result = {
            url,
            title: productTitle,
            price,
            originalPrice: promotionData.originalPrice,
            promotionType: promotionData.promotionType,
            promotionText: promotionData.promotionText,
            promotionDiscountValue: promotionData.promotionDiscountValue,
            scrapedAt: new Date().toISOString(),
        };

        log.info(`Scraped: ${productTitle} - €${price}${promotionData.promotionType ? ` (${promotionData.promotionText})` : ''}`);

        // Save to dataset
        await Dataset.pushData(result);
    },

    // Handle failures
    failedRequestHandler({ request, log }) {
        log.error(`Failed to scrape: ${request.url}`);
    },
});

// Add URLs to the crawler
await crawler.addRequests(urls.map((url) => ({ url })));

// Run the crawler
await crawler.run();

console.log('Crawler finished.');

await Actor.exit();
