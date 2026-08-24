from __future__ import annotations

"""
Image Utilities
────────────────
Helpers for preparing images for vision-API classification.

`fetch_and_downsample` fetches a photo URL, downsamples it to a small JPEG,
and returns it base64-encoded — this cuts the bytes (and therefore the input
tokens) sent to the classifier dramatically vs. shipping full-res dealer photos.
"""

import base64
from io import BytesIO
from typing import Optional

import httpx
from PIL import Image


async def fetch_and_downsample(
    url: str,
    max_dimension: int = 512,
    quality: int = 80,
) -> Optional[str]:
    """
    Fetch an image URL, downsample to max_dimension on the longest side,
    return as base64-encoded JPEG string ready for vision API input.
    Returns None on any failure — caller should skip that photo, not crash the batch.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None

            img = Image.open(BytesIO(resp.content))

            # Convert to RGB if needed (handles PNG with alpha, CMYK, etc.)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            # Downsample preserving aspect ratio
            img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)

            return base64.b64encode(buffer.getvalue()).decode("utf-8")

    except Exception as e:
        print(f"Downsample failed for {url}: {e}")
        return None
