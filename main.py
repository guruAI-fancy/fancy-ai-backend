"""Fancy AI Stage 2: cautious, metadata-only product-link preview backend.

Does not bypass store access controls. Does not take, upload, or store person images.
For production, replace page metadata reads with authorized merchant APIs.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Fancy AI Product Preview", version="2.0")

STORES = {
    "Amazon": ("amazon.in", "amazon.com", "amzn.in", "amzn.to"),
    "Flipkart": ("flipkart.com", "fkrt.it"),
    "Myntra": ("myntra.com",),
    "AJIO": ("ajio.com",),
    "Meesho": ("meesho.com",),
}
MAX_HTML_BYTES = 1_500_000
MAX_HOPS = 4

class ProductRequest(BaseModel):
    url: str = Field(min_length=10, max_length=5000)


def validate_product_url(text: str) -> tuple[str, str]:
    url = text.strip()
    if not re.match(r"^https://", url, flags=re.I):
        raise ValueError("HTTPS product link required.")
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or (parsed.port and parsed.port != 443):
        raise ValueError("Invalid product link.")
    if not any(segment for segment in parsed.path.split("/") if segment):
        raise ValueError("Please paste a product link, not the store home page.")
    host = parsed.hostname.rstrip(".").lower()
    for store, roots in STORES.items():
        if any(host == root or host.endswith("." + root) for root in roots):
            return store, parsed.geturl()
    raise ValueError("Supported stores: Amazon, Flipkart, Myntra, AJIO, Meesho.")


async def ensure_public_host(url: str) -> None:
    """Guard against localhost/private addresses even after redirects.

    Normal DNS rebinding protections require egress firewall controls as well;
    only known merchant domains are allowed by validate_product_url.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError("Missing hostname")
    addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("DNS lookup failed")
    for info in addresses:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Unsafe network destination")


def text_clean(value: object, max_chars: int = 240) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or len(text) > 10000:
        return None
    return text[:max_chars]


def find_product_jsonld(soup: BeautifulSoup) -> dict:
    def walk(obj):
        if isinstance(obj, list):
            for entry in obj:
                result = walk(entry)
                if result: return result
        if isinstance(obj, dict):
            kind = obj.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(kind, str) and kind.lower() == "product" for kind in kinds):
                return obj
            for subkey in ("@graph", "mainEntity", "itemListElement"):
                if subkey in obj:
                    found = walk(obj[subkey])
                    if found: return found
        return {}
    for tag in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            loaded = json.loads(tag.get_text()[:200_000])
            found = walk(loaded)
            if found: return found
        except (TypeError, ValueError):
            pass
    return {}


def metadata_from_html(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    metas = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        value = tag.get("content")
        if key and value and key not in metas: metas[key] = value.strip()
    data = find_product_jsonld(soup)

    name = (data.get("name") if isinstance(data.get("name"), str) else None)
    title = text_clean(name or metas.get("og:title") or metas.get("twitter:title") or (soup.title.get_text() if soup.title else None))
    generic = {"amazon.in", "amazon.com", "flipkart", "flipkart.com", "myntra", "ajio", "meesho", "access denied", "robot check", "captcha", "just a moment..."}
    if title and title.lower().strip(" .") in generic: title = None

    image = data.get("image") or metas.get("og:image:secure_url") or metas.get("og:image") or metas.get("twitter:image")
    if isinstance(image, list): image = next((v for v in image if isinstance(v, str)), None)
    if isinstance(image, dict): image = image.get("url")
    image_url = None
    if isinstance(image, str):
        target = urljoin(base_url, image.strip())
        if urlparse(target).scheme == "https" and urlparse(target).hostname:
            image_url = target[:2000]

    offer = data.get("offers")
    if isinstance(offer, list): offer = next((v for v in offer if isinstance(v, dict)), None)
    if not isinstance(offer, dict): offer = {}
    price = offer.get("price") or metas.get("product:price:amount") or metas.get("og:price:amount")
    if isinstance(price, (float, int)):
        price = str(price)
    price = text_clean(price, 30) if price is not None else None
    if price and not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", price): price = None
    currency = text_clean(offer.get("priceCurrency") or metas.get("product:price:currency"), 8)
    if currency and not re.fullmatch(r"[A-Za-z]{3}", currency): currency = None

    return {"title": title, "image_url": image_url, "price": price, "currency": currency}


async def fetch_metadata(original_url: str) -> tuple[str, dict]:
    current = original_url
    async with httpx.AsyncClient(timeout=httpx.Timeout(13, connect=6), follow_redirects=False,
                                 trust_env=False, headers={"User-Agent": "FancyAIProductPreview/1.0", "Accept": "text/html"}) as client:
        for _ in range(MAX_HOPS + 1):
            validate_product_url(current)
            await ensure_public_host(current)
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location: raise ValueError("Store redirect missing location.")
                    target = urljoin(current, location)
                    validate_product_url(target)
                    current = target
                    continue
                if response.status_code in (401, 403, 429):
                    return "blocked", {}
                if response.status_code != 200:
                    return "unavailable", {}
                ct = response.headers.get("content-type", "").lower()
                if "text/html" not in ct: return "unavailable", {}
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_HTML_BYTES: return "unavailable", {}
                charset = response.encoding or "utf-8"
                html = raw.decode(charset, errors="replace")
                return "ok", metadata_from_html(html, current)
    return "unavailable", {}


@app.get("/health")
def health():
    return {"status": "ok", "service": "fancy-ai-product-preview", "photos_uploaded": False}


@app.post("/api/product/analyze")
async def analyze(payload: ProductRequest):
    try:
        platform, url = validate_product_url(payload.url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    try:
        fetch_status, metadata = await fetch_metadata(url)
    except (httpx.RequestError, OSError, ValueError, UnicodeError):
        fetch_status, metadata = "unavailable", {}
    if metadata.get("title") or metadata.get("image_url"):
        status = "found"
        message = "Public product metadata found. Confirm image, colour, size and price with the store."
    elif fetch_status == "blocked":
        status = "blocked"
        message = "Store blocked automatic preview. Upload a dress screenshot instead."
    else:
        status = "unavailable"
        message = "This store did not provide usable product metadata. Upload a dress screenshot instead."
    return {
        "status": status,
        "platform": platform,
        "url": url,
        "title": metadata.get("title"),
        "image_url": metadata.get("image_url"),
        "price": metadata.get("price"),
        "currency": metadata.get("currency"),
        "source": "public_html_metadata" if status == "found" else None,
        "message": message,
    }
