"""
Shared primitives for Lidl discovery scripts (MASA-140 / Phase 1.1 of MASA-137).

Extracted from `discover_lidl_aliases.py` so the upcoming sitemap-import
pipeline (`discover_lidl_sitemap_import.py`) can reuse them without copy-paste.

Module surface area (all importable):

  Constants
    LIDL_SITEMAP_URL, USER_AGENT, CACHE_DIR, CACHE_TTL_SECONDS,
    LIVE_FETCH_MIN_DELAY, LIVE_FETCH_MAX_DELAY, HTTP_TIMEOUT

  Brand sets
    LIDL_OWN_BRANDS, COMPETING_BRANDS, KNOWN_BRAND_TOKENS, VARIANT_TOKENS

  Regexes
    SIZE_RE, MULTIPACK_RE, RATING_RE, FILLER, PORTION_GUARD_RE,
    HTML_MULTIPACK_RE, HTML_WEIGHT_VOLUME_RE, HTML_PACK_RE

  Helpers
    normalise, extract_size_from_text, product_size, variant_tokens,
    fetch_lidl_sitemap_urls, _cache_path_for, fetch_lidl_page,
    _visible_spans, extract_size_from_html, extract_page_text_signals,
    _brand_in_norm, _slug_brand_token, _brand_mismatch_reason
"""
from __future__ import annotations

import gzip
import json
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
LIDL_SITEMAP_URL = "https://www.lidl.ie/p/export/IE/en/product_sitemap.xml.gz"
USER_AGENT = "Mozilla/5.0 (compatible; MasterMarket-Discovery/0.2)"
CACHE_DIR = Path.home() / ".cache" / "mastermarket" / "lidl_html"
CACHE_TTL_SECONDS = 24 * 3600
LIVE_FETCH_MIN_DELAY = 0.5
LIVE_FETCH_MAX_DELAY = 1.5
HTTP_TIMEOUT = 15

# --------------------------------------------------------------------------- #
# Regexes — same normalisation rules as discover_aldi_aliases.py.
# --------------------------------------------------------------------------- #
SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(g|kg|ml|l|ltr|litre|liter|cl|oz|pk|pack)\b",
    re.I,
)
MULTIPACK_RE = re.compile(r"\b(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|ml|kg)\b", re.I)
RATING_RE = re.compile(r"\d+\.?\d*\s*stars?\s*\(.*?\)", re.I)
FILLER = re.compile(
    r"\b(the|a|an|of|in|with|and|&|for|from|style|original|classic)\b", re.I
)

# Detect "garbage" unit fields on the MM side (rule #5 from the issue spec).
PORTION_GUARD_RE = re.compile(
    r"\b(portion|serving|cup|glass|slice|bowl)\b", re.I
)

# HTML size patterns — ordered: multipack first (most specific),
# then weight/volume, then pack-count.
HTML_MULTIPACK_RE = re.compile(
    r"\b(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(g|ml|kg|l|cl)\b", re.I
)
HTML_WEIGHT_VOLUME_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(kg|g|ml|cl|l|ltr|litre|liter)\b", re.I
)
HTML_PACK_RE = re.compile(r"\b(\d+)\s*(pack|pk)\b", re.I)

# Variant tokens we use to gate ambiguous matches against the Lidl page title.
VARIANT_TOKENS = (
    "light",
    "real",
    "regular",
    "diet",
    "zero",
    "decaf",
    "organic",
    "salted",
    "unsalted",
    "spreadable",
    "low",
    "fat",
    "skim",
    "skimmed",
    "whole",
    "smooth",
    "crunchy",
)

# --------------------------------------------------------------------------- #
# Brand sets (MASA-135)
# --------------------------------------------------------------------------- #
# Lidl exclusive (private-label) brands we want to onboard. Lower-case, since
# we always match against `normalise()`-d strings.
LIDL_OWN_BRANDS = (
    "milbona",
    "italiamo",
    "vemondo",
    "combino",
    "solevita",
    "newgate",
    "lupilu",
    "dulano",
    "pilos",
    "trattoria verdi",
)

# Known competing brands that should never appear in a Lidl-exclusive's slug,
# nor in the slug of a *different* national brand we're matching. Start small
# (the issue says "grow with rejections"). All values lower-case and
# `normalise()`-friendly.
COMPETING_BRANDS = (
    "alpro",
    "coca cola",
    "hellmann",
    "kelloggs",
    "kerrygold",
    "pringles",
    "tayto",
    "dolmio",
    "nestle",
    "heinz",
    "mccain",
    "barilla",
)

# Master set of "known brand tokens" — used to detect the brand a Lidl slug
# is advertising. We normalise both sides (slug and brand) with the same
# pipeline, so multi-word brands like "coca cola" and "trattoria verdi" land
# as space-separated lower-case tokens that survive substring matching.
KNOWN_BRAND_TOKENS = tuple(
    sorted(set(LIDL_OWN_BRANDS) | set(COMPETING_BRANDS), key=len, reverse=True)
)


# --------------------------------------------------------------------------- #
# Normalisation + product-side size derivation
# --------------------------------------------------------------------------- #
def normalise(text: str) -> str:
    text = text.lower().strip()
    text = MULTIPACK_RE.sub("", text)
    text = SIZE_RE.sub("", text)
    text = RATING_RE.sub("", text)
    text = FILLER.sub("", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_size_from_text(text: str):
    """Generic helper used for sitemap slugs (legacy parity)."""
    if not text:
        return None
    m_multi = MULTIPACK_RE.search(text)
    if m_multi:
        return f"{m_multi.group(1)}x{m_multi.group(2).lower()}{m_multi.group(3).lower()}"
    m = SIZE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).lower()}{m.group(2).lower()}"


def product_size(name: str, unit: str):
    """
    Derive a usable size for an MM product.

    Priority:
      1. From the product `name` (most reliable — curated by data team).
      2. From `unit`, but only if it does NOT contain portion/serving/cup/etc.
         (those are brewed-tea / serving descriptions, not pack sizes).
    Returns None if nothing trustworthy is available.
    """
    s = extract_size_from_text(name or "")
    if s:
        return s
    if unit and not PORTION_GUARD_RE.search(unit):
        return extract_size_from_text(unit)
    return None


def variant_tokens(text: str) -> set:
    if not text:
        return set()
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    return tokens & set(VARIANT_TOKENS)


# --------------------------------------------------------------------------- #
# Sitemap fetcher
# --------------------------------------------------------------------------- #
def fetch_lidl_sitemap_urls():
    req = urllib.request.Request(LIDL_SITEMAP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        gz = resp.read()
    raw = gzip.decompress(gz).decode("utf-8", errors="replace")
    urls = re.findall(r"<loc>([^<]+)</loc>", raw)
    out = []
    for u in urls:
        m = re.search(r"/p/([^/]+)/p(\d+)", u)
        if not m:
            continue
        slug = m.group(1).replace("-", " ")
        sku = m.group(2)
        out.append(
            {
                "url": u,
                "slug": slug,
                "sku": sku,
                "norm": normalise(slug),
                "slug_size": extract_size_from_text(slug),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# HTTP layer with 24h disk cache + polite delay between LIVE fetches
# --------------------------------------------------------------------------- #
def _cache_path_for(url: str) -> Path:
    sku_match = re.search(r"/p(\d+)\b", url)
    key = sku_match.group(1) if sku_match else re.sub(r"[^a-z0-9]+", "_", url.lower())
    return CACHE_DIR / f"{key}.html"


def fetch_lidl_page(url: str, fetch_log: dict) -> str | None:
    """Returns the HTML body, or None on failure. Caches to disk for 24h."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for(url)
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            fetch_log["cache_hits"] = fetch_log.get("cache_hits", 0) + 1
            return cache_path.read_text(encoding="utf-8", errors="replace")

    fetch_log["live_fetch_attempts"] = fetch_log.get("live_fetch_attempts", 0) + 1
    delay = random.uniform(LIVE_FETCH_MIN_DELAY, LIVE_FETCH_MAX_DELAY)
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        cache_path.write_text(body, encoding="utf-8")
        return body
    except Exception as e:
        fetch_log["live_fetch_failures"] = fetch_log.get("live_fetch_failures", 0) + 1
        fetch_log.setdefault("failure_samples", []).append(
            {"url": url, "error": f"{type(e).__name__}: {e}"}
        )
        return None


# --------------------------------------------------------------------------- #
# HTML parsing — extract size + variant tokens from a Lidl page
# --------------------------------------------------------------------------- #
def _visible_spans(html: str) -> list[str]:
    return [
        s.strip()
        for s in re.findall(r"<span[^>]*>([^<]{1,200})</span>", html)
        if s.strip()
    ]


def extract_size_from_html(html: str) -> str | None:
    """
    Try in order: multipack → weight/volume → pack-count.
    Look first inside visible <span> content; fall back to whole HTML
    only if nothing trips on spans (avoids picking up sizes from script
    blocks, UTM params, etc.).
    """
    spans = _visible_spans(html)
    span_text = " | ".join(spans)
    for source in (span_text, html):
        m = HTML_MULTIPACK_RE.search(source)
        if m:
            return f"{m.group(1)}x{m.group(2).lower()}{m.group(3).lower()}"
        m = HTML_WEIGHT_VOLUME_RE.search(source)
        if m:
            unit = m.group(2).lower()
            unit = "l" if unit in ("ltr", "litre", "liter") else unit
            return f"{m.group(1).lower()}{unit}"
        m = HTML_PACK_RE.search(source)
        if m:
            return f"{m.group(1)}pack"
    return None


def extract_page_text_signals(html: str) -> dict:
    """
    Return tokens we can use to disambiguate collisions. We pull from:
      - <title>...</title>
      - JSON-LD `name`
      - The first <h1> if present (some Lidl skins render it)
    """
    bits = []
    m = re.search(r"<title[^>]*>([^<]{1,300})</title>", html)
    if m:
        bits.append(m.group(1))
    m = re.search(r"<h1[^>]*>([^<]{1,300})</h1>", html)
    if m:
        bits.append(m.group(1))
    for ld in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            obj = json.loads(ld.group(1))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("@type") in ("Product", "ProductGroup"):
            n = obj.get("name")
            if isinstance(n, str):
                bits.append(n)
            d = obj.get("description")
            if isinstance(d, str):
                bits.append(d)
    text = " ".join(bits)
    return {"text": text, "variant": variant_tokens(text)}


# --------------------------------------------------------------------------- #
# Brand-mismatch helpers (Phase 1.5 / MASA-135)
# --------------------------------------------------------------------------- #
def _brand_in_norm(brand_token: str, norm_text: str) -> bool:
    """
    Return True if `brand_token` appears as a whole-word match in
    space-tokenised `norm_text`. Multi-word brands ("coca cola",
    "trattoria verdi") match against the joined string.
    """
    if not brand_token or not norm_text:
        return False
    if " " in brand_token:
        # Multi-word: substring on " " padded boundaries.
        return f" {brand_token} " in f" {norm_text} "
    return brand_token in norm_text.split()


def _slug_brand_token(sitemap_norm: str) -> str | None:
    """
    Return the first known brand token that appears in the (normalised) Lidl
    slug. Iteration order is longest-token-first (see KNOWN_BRAND_TOKENS) so
    that "trattoria verdi" wins over "verdi" if both were known.
    """
    for tok in KNOWN_BRAND_TOKENS:
        if _brand_in_norm(tok, sitemap_norm):
            return tok
    return None


def _brand_mismatch_reason(product_brand: str, sitemap_norm: str) -> str | None:
    """
    Decide whether a (product, sitemap-entry) pair must be hard-rejected
    on the brand axis BEFORE Phase-2 HTML fetch.

    Returns:
        None if the brand is consistent (or no signal either way).
        "competing_brand_in_slug" if the slug advertises a *different* known
        brand than the MM product's brand.

    Symmetric design:
      * MM brand is a Lidl exclusive (e.g. Vemondo) — slug must not contain
        any competing national brand. Catches Vemondo→Alpro.
      * MM brand is a national brand — slug must not contain a *different*
        known brand. Catches Hellmann's vs Heinz collisions.
    """
    slug_tok = _slug_brand_token(sitemap_norm)
    if slug_tok is None:
        # Slug doesn't advertise a known brand — let Phase-2 size/variant
        # decide. This keeps the filter conservative (no false positives on
        # generic product slugs like "p/oat-milk-1l").
        return None

    mm_brand_norm = normalise(product_brand or "")
    if not mm_brand_norm:
        # No brand on MM side → can't safely match a brand-bearing slug.
        return "competing_brand_in_slug"

    # Same-brand match → keep.
    if _brand_in_norm(slug_tok, mm_brand_norm):
        return None

    # MM brand differs from the brand the slug is advertising → hard reject.
    return "competing_brand_in_slug"
