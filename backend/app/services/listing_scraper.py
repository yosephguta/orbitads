from __future__ import annotations

"""
Listing Scraper Service
────────────────────────
Extracts car photos from dealership listing URLs.

Strategy:
  1. Scrape the provided URL directly with Playwright
  2. If we get 3+ good photos, return them
  3. If not, search Cars.com / CarGurus for the VIN
  4. Scrape photos from whichever source works

Photo filtering:
  - Minimum dimensions (reject thumbnails)
  - Valid image formats only
  - Deduplicate
  - Prefer exterior shots over interior

Returns 3-5 photo URLs of the exact car being sold.
"""

import re
import asyncio
from urllib.parse import urlparse, urljoin
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


# ── URL quality filters ───────────────────────────────────────
# These patterns indicate thumbnails or non-car images — skip them
SKIP_PATTERNS = [
    "thumb", "thumbnail", "icon", "logo", "badge", "avatar",
    "sprite", "placeholder", "blank", "pixel", "tracking",
    "dealer-logo", "brand-logo", "background",
    ".gif", "data:image",
]

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Patterns that suggest exterior photos — prefer these
EXTERIOR_PATTERNS = [
    "exterior", "front", "side", "rear", "back", "outside",
    "_01", "_02", "_03", "_1.", "_2.", "_3.",
]


def _is_valid_photo_url(url: str) -> bool:
    """Return True if the URL looks like a real car photo."""
    url_lower = url.lower()

    # Must be http/https
    if not url_lower.startswith("http"):
        return False

    # Skip known bad patterns
    if any(pat in url_lower for pat in SKIP_PATTERNS):
        return False

    # Must have a valid image extension OR be from a known photo CDN
    has_valid_ext = any(ext in url_lower for ext in VALID_EXTENSIONS)
    is_known_cdn  = any(cdn in url_lower for cdn in [
        "cstatic-images.com",   # Cars.com CDN
        "cargurus.com",
        "autotrader.com",
        "dealer.com",
        "dealerfire.com",
        "izmocars.com",
        "vinsolutions.com",
        "dealerinspire.com",
        "flick fusion",
        "homenetiol.com",
        "imgix.net",
        "cloudfront.net",
        "amazonaws.com",
    ])

    return has_valid_ext or is_known_cdn


def _score_photo_url(url: str) -> int:
    """
    Score a photo URL — higher is better.
    Exterior photos score highest, then interior, then other.
    """
    url_lower = url.lower()
    score = 0

    # Prefer larger image variants
    if any(s in url_lower for s in ["xxlarge", "xlarge", "large", "full", "hd"]):
        score += 10
    if any(s in url_lower for s in ["small", "medium", "thumb"]):
        score -= 5

    # Prefer exterior photos
    if any(pat in url_lower for pat in EXTERIOR_PATTERNS):
        score += 5

    return score


def _deduplicate_photos(urls: list[str]) -> list[str]:
    """
    Remove near-duplicate URLs.
    Cars.com often has the same photo in multiple sizes — keep the largest.
    """
    seen_bases = set()
    unique = []

    for url in urls:
        # Extract the base filename without size suffix
        # e.g. "photo_large.jpg" and "photo_thumb.jpg" → same base "photo"
        base = re.sub(r'[-_](thumb|small|medium|large|xlarge|xxlarge)', '', url.lower())
        base = re.sub(r'\?.*$', '', base)  # strip query params

        if base not in seen_bases:
            seen_bases.add(base)
            unique.append(url)

    return unique


# ── Main scraper ──────────────────────────────────────────────
async def get_listing_photos(
    listing_url: str,
    vin: Optional[str] = None,
    max_photos: int = 5,
) -> list[str]:
    """
    Get car photos from a listing URL.

    Args:
        listing_url: The dealership listing URL
        vin:         VIN number (used for fallback search)
        max_photos:  Maximum photos to return (default 5)

    Returns:
        List of photo URLs for the exact car

    Raises:
        RuntimeError if no photos can be found
    """
    # Try scraping the listing URL directly
    photos = await _scrape_url(listing_url)

    if len(photos) >= 3:
        return photos[:max_photos]

    # Not enough photos — try fallback sources using the VIN
    if vin:
        print(f"Direct scrape got {len(photos)} photos. Trying fallback sources...")

        # Try Cars.com
        cars_photos = await _search_cars_com(vin)
        if len(cars_photos) >= 3:
            return cars_photos[:max_photos]

        # Try CarGurus
        cargurus_photos = await _search_cargurus(vin)
        if len(cargurus_photos) >= 3:
            return cargurus_photos[:max_photos]

    # Combine whatever we have
    all_photos = photos
    if len(all_photos) >= 1:
        return all_photos[:max_photos]

    raise RuntimeError(
        f"Could not find photos for this listing. "
        f"URL: {listing_url}, VIN: {vin}. "
        f"Please upload photos manually."
    )


async def _scrape_url(url: str, wait_seconds: int = 3) -> list[str]:
    """
    Scrape photos from any URL using Playwright.
    Renders JavaScript so modern dealership sites work correctly.
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Set a real browser user agent to avoid bot detection
            await page.set_extra_http_headers({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })

            # Navigate to the page
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait a bit for lazy-loaded images to appear
            await asyncio.sleep(wait_seconds)

            # Scroll down to trigger lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(1)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

            # Extract all image URLs from the page
            photo_urls = await page.evaluate("""
                () => {
                    const urls = new Set();

                    // Standard img tags
                    document.querySelectorAll('img').forEach(img => {
                        if (img.src) urls.add(img.src);
                        if (img.dataset.src) urls.add(img.dataset.src);
                        if (img.dataset.lazySrc) urls.add(img.dataset.lazySrc);
                    });

                    // Background images in style attributes
                    document.querySelectorAll('[style*="background"]').forEach(el => {
                        const match = el.style.backgroundImage.match(/url\\(['"]?([^'"\\)]+)/);
                        if (match) urls.add(match[1]);
                    });

                    // Source tags inside picture elements
                    document.querySelectorAll('source').forEach(src => {
                        if (src.srcset) {
                            src.srcset.split(',').forEach(s => {
                                const url = s.trim().split(' ')[0];
                                if (url) urls.add(url);
                            });
                        }
                    });

                    return Array.from(urls);
                }
            """)

            await browser.close()

            # Filter and score the photos
            valid = [u for u in photo_urls if _is_valid_photo_url(u)]
            unique = _deduplicate_photos(valid)
            scored = sorted(unique, key=_score_photo_url, reverse=True)

            return scored

    except PlaywrightTimeout:
        print(f"Playwright timed out scraping: {url}")
        return []
    except Exception as e:
        print(f"Playwright scraping failed for {url}: {e}")
        return []


async def _search_cars_com(vin: str) -> list[str]:
    """Search Cars.com for a specific VIN and scrape photos."""
    search_url = f"https://www.cars.com/shopping/results/?keyword={vin}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_extra_http_headers({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)

            # Find the first listing link
            listing_link = await page.evaluate("""
                () => {
                    const link = document.querySelector(
                        'a.image-gallery-link, a[data-qa="vehicle-card-link"]'
                    );
                    return link ? link.href : null;
                }
            """)

            await browser.close()

            if listing_link:
                print(f"Found Cars.com listing: {listing_link}")
                return await _scrape_url(listing_link)

    except Exception as e:
        print(f"Cars.com search failed for VIN {vin}: {e}")

    return []


async def _search_cargurus(vin: str) -> list[str]:
    """Search CarGurus for a specific VIN and scrape photos."""
    search_url = f"https://www.cargurus.com/Cars/new/nl#listing={vin}"
    try:
        # CarGurus search by VIN
        search_url = (
            f"https://www.cargurus.com/Cars/new/nl#search="
            f"%7B%22zip%22%3A%2221061%22%2C%22trim%22%3A%22{vin}%22%7D"
        )
        photos = await _scrape_url(search_url, wait_seconds=4)
        if photos:
            return photos

    except Exception as e:
        print(f"CarGurus search failed for VIN {vin}: {e}")

    return []