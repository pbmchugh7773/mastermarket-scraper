"""Shared helpers — JSON-LD extraction + field helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def iter_jsonld(html: str) -> Iterable[Any]:
    """Yield each parsed JSON-LD document in the page."""
    for match in JSONLD_BLOCK_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield parsed


def flatten_graph(doc: Any) -> Iterable[dict]:
    """Flatten a JSON-LD doc to individual nodes (handles arrays + @graph)."""
    if isinstance(doc, list):
        for item in doc:
            yield from flatten_graph(item)
        return
    if not isinstance(doc, dict):
        return
    if "@graph" in doc and isinstance(doc["@graph"], list):
        for item in doc["@graph"]:
            yield from flatten_graph(item)
        return
    yield doc


def find_product_node(html: str) -> Optional[dict]:
    """Return the first JSON-LD Product node in the page, or None."""
    for doc in iter_jsonld(html):
        for node in flatten_graph(doc):
            node_type = node.get("@type")
            if isinstance(node_type, list):
                if "Product" in node_type:
                    return node
            elif node_type == "Product":
                return node
    return None


def find_webpage_node(html: str) -> Optional[dict]:
    """Return the first JSON-LD WebPage node — useful when Product is missing."""
    for doc in iter_jsonld(html):
        for node in flatten_graph(doc):
            node_type = node.get("@type")
            if isinstance(node_type, list):
                if "WebPage" in node_type:
                    return node
            elif node_type == "WebPage":
                return node
    return None


def first_offer_price(product_node: dict) -> Optional[float]:
    """Extract a float price from a Product node's `offers` field."""
    offers = product_node.get("offers")
    if not offers:
        return None
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    price = offers.get("price") or offers.get("lowPrice")
    if price is None:
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def image_url(product_node: dict) -> Optional[str]:
    img = product_node.get("image")
    if isinstance(img, str):
        return img
    if isinstance(img, list) and img:
        first = img[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("contentUrl")
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl")
    return None


SIZE_PATTERNS = [
    re.compile(r"\b(\d+(?:[.,]\d+)?)\s?(kg|g|ml|l|ltr|litre|pack|piece|each|cl)\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s?x\s?\d+(?:[.,]\d+)?\s?(kg|g|ml|l|ltr|cl)\b", re.IGNORECASE),
]


def guess_size_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in SIZE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None
