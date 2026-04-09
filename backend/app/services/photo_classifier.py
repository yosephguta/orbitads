from __future__ import annotations

"""
Photo Classifier Service
─────────────────────────
Uses Claude's vision API to classify car photos by angle/position,
then sorts them into a natural walkaround sequence.

This transforms a random pile of 37 dealer photos into:
  exterior_front → exterior_front_right → exterior_right →
  exterior_rear_right → exterior_rear → exterior_rear_left →
  exterior_left → interior_dashboard → interior_seats → interior_cargo

The sorted sequence is used by Shotstack to build a walkaround-style video.

Cost: ~$0.003 per image. Classifying 15 photos costs ~$0.04 per car.
"""

import asyncio
import base64
import httpx
import json
from typing import Optional

import anthropic

from app.core.config import get_settings

settings = get_settings()

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# ── Walkaround sequence order ─────────────────────────────────
# Photos will be sorted by their position in this list.
# Lower index = appears earlier in the walkaround video.
WALKAROUND_ORDER = [
    "exterior_front",
    "exterior_front_right",
    "exterior_right",
    "exterior_rear_right",
    "exterior_rear",
    "exterior_rear_left",
    "exterior_left",
    "exterior_front_left",
    "exterior_detail",      # ← add this
    "interior_dashboard",
    "interior_seats",
    "interior_cargo",
    "interior_detail",
    "other",
]

# Valid categories Claude can return
VALID_CATEGORIES = set(WALKAROUND_ORDER)


# ── Single photo classification ───────────────────────────────
async def classify_photo(image_url: str) -> str:
    """
    Classify a single car photo by its angle/position.

    Args:
        image_url: Public URL of the photo

    Returns:
        One of the WALKAROUND_ORDER category strings
    """

 # ── Pre-filter known junk URLs ────────────────────────────
    url_lower = image_url.lower()
    skip_patterns = [
        'valuebadge', 'showme', 'carfax', 'autocheck',
        'logo', 'badge', 'iv.png', 'videoplayer',
        'dealervideopro', 'showme.svg',
    ]
    if any(p in url_lower for p in skip_patterns):
        return "other"

    # ── Skip non-image file types ─────────────────────────────
    if url_lower.endswith('.png') and any(p in url_lower for p in ['badge', 'logo', 'icon']):
        return "other"

    try:
        # Download the image bytes
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(image_url)
            if resp.status_code != 200:
                return "other"
            image_bytes = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")
            # Normalize content type
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            else:
                media_type = "image/jpeg"
                
        url_lower = image_url.lower()
        skip_patterns = [
        'valuebadge', 'showme', 'carfax', 'autocheck',
        'logo', 'badge', 'iv.png', 'videoplayer',
        'dealervideopro',
        ]

        if any(p in url_lower for p in skip_patterns):
            return "other"

        # Encode as base64 for Claude
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

        # Ask Claude to classify the photo
        message = await _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=20,  # we only need one word back
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                         "text": (
                            "Classify this car photo with exactly one of these labels:\n\n"
                            "FULL CAR EXTERIOR (most of car body visible):\n"
                            "exterior_front, exterior_front_right, exterior_right, "
                            "exterior_rear_right, exterior_rear, exterior_rear_left, "
                            "exterior_left, exterior_front_left\n\n"
                            "CLOSE-UP EXTERIOR DETAILS (zoomed in on one part):\n"
                            "exterior_detail\n\n"
                            "INTERIOR:\n"
                            "interior_dashboard, interior_seats, interior_cargo, interior_detail\n\n"
                            "NOT A CAR PHOTO:\n"
                            "other\n\n"
                            "Rules:\n"
                            "- exterior_* (not exterior_detail) = you can see at least half the car body\n"
                            "- exterior_detail = close-up of wheel, tire, mirror, light, badge, trim piece\n"
                            "- interior_dashboard = steering wheel area, infotainment, gauges\n"
                            "- interior_seats = seats, headrests, upholstery\n"
                            "- interior_cargo = trunk, cargo area\n"
                            "- interior_detail = console, door panel, controls, buttons, any other interior\n"
                            "- other = logos, dealership signs, window stickers, price sheets, QR codes\n"
                            "- When in doubt between interior_detail and other, choose interior_detail\n\n"
                            "Reply with only the label, nothing else."
                        ),
                        },
                    ],
                }
            ],
        )

        label = message.content[0].text.strip().lower()

        # Validate the response
        if label in VALID_CATEGORIES:
            return label
        return "other"

    except Exception as e:
        print(f"Photo classification failed for {image_url}: {e}")
        return "other"


# ── Batch classification ──────────────────────────────────────
async def classify_photos_batch(
    photo_urls: list[str],
    max_photos: int = 20,
    concurrency: int = 3,
) -> list[dict]:
    """
    Classify multiple photos concurrently and return them with labels.

    Args:
        photo_urls:  List of photo URLs to classify
        max_photos:  Maximum number to classify (saves cost)
        concurrency: How many to classify simultaneously

    Returns:
        List of dicts: [{"url": "...", "label": "exterior_front"}, ...]
    """
    # Only classify up to max_photos
    urls_to_classify = photo_urls[:max_photos]

    # Use a semaphore to limit concurrent API calls
    sem = asyncio.Semaphore(concurrency)

    async def classify_with_sem(url: str) -> dict:
        async with sem:
            label = await classify_photo(url)
            return {"url": url, "label": label}

    results = await asyncio.gather(
        *[classify_with_sem(url) for url in urls_to_classify]
    )

    return list(results)


# ── Sort into walkaround sequence ─────────────────────────────
def sort_into_walkaround(classified_photos: list[dict]) -> list[dict]:
    """
    Sort classified photos into the natural walkaround sequence.
    Within each category, preserve the original order.
    """
    # Group photos by category
    by_category: dict[str, list[dict]] = {cat: [] for cat in WALKAROUND_ORDER}
    for photo in classified_photos:
        label = photo.get("label", "other")
        if label in by_category:
            by_category[label].append(photo)
        else:
            by_category["other"].append(photo)

    # Flatten in walkaround order
    sorted_photos = []
    for category in WALKAROUND_ORDER:
        sorted_photos.extend(by_category[category])

    return sorted_photos


# ── Select best photos for video ──────────────────────────────
def select_video_photos(
    sorted_photos: list[dict],
    exterior_count: int = 5,
    interior_count: int = 2,
) -> list[str]:
    """
    Pick the best photos for the video from the sorted walkaround sequence.
    Returns a flat list of URLs in video order.

    Args:
        sorted_photos:   Output of sort_into_walkaround()
        exterior_count:  How many exterior shots to include
        interior_count:  How many interior shots to include

    Returns:
        List of photo URLs ready for Shotstack
    """
    exterior_categories = {
        "exterior_front", "exterior_front_right", "exterior_right",
        "exterior_rear_right", "exterior_rear", "exterior_rear_left",
        "exterior_left", "exterior_front_left",
    }
    interior_categories = {
        "interior_dashboard", "interior_seats",
        "interior_cargo", "interior_detail",
    }

    exterior_photos = [
        p for p in sorted_photos if p["label"] in exterior_categories
    ]
    interior_photos = [
        p for p in sorted_photos if p["label"] in interior_categories
    ]

    selected = (
        [p["url"] for p in exterior_photos[:exterior_count]] +
        [p["url"] for p in interior_photos[:interior_count]]
    )

    return selected


# ── Main entry point ──────────────────────────────────────────
async def get_walkaround_photos(
    photo_urls: list[str],
    exterior_count: int = 5,
    interior_count: int = 2,
) -> list[str]:
    """
    Full pipeline: classify → sort → select.
    Returns a list of photo URLs in walkaround order, ready for Shotstack.

    Args:
        photo_urls:     All scraped photo URLs (can be 30+)
        exterior_count: Exterior shots for the video
        interior_count: Interior shots for the video

    Returns:
        Ordered list of photo URLs for video assembly
    """
    if not photo_urls:
        return []

    print(f"Classifying {min(len(photo_urls), 20)} of {len(photo_urls)} photos...")

    # Classify
    classified = await classify_photos_batch(photo_urls, max_photos=20)

    print("Classification results:")
    for p in classified:
        print(f"  {p['label']:25s} {p['url'].split('/')[-1]}")

    # Sort into walkaround order
    sorted_photos = sort_into_walkaround(classified)

    # Select best for video
    selected = select_video_photos(sorted_photos, exterior_count, interior_count)

    print(f"Selected {len(selected)} photos for video: {[p.split('/')[-1] for p in selected]}")

    return selected