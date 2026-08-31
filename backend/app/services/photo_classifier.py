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
from datetime import datetime, timedelta
from typing import Optional

import re

import anthropic
from sqlalchemy import delete

from app.core.config import get_settings
from app.services.image_utils import fetch_and_downsample

settings = get_settings()

# ── Cache write/purge tuning ──────────────────────────────────
# Cars rarely stay on a lot longer than ~2 months, so cache entries older than
# this are useless — purge them. The purge is throttled to run at most once per
# PURGE_INTERVAL and always happens in a fire-and-forget background task, so it
# never adds latency to a classify request.
CACHE_TTL = timedelta(days=60)
PURGE_INTERVAL = timedelta(hours=24)
_last_cache_purge_at: Optional[datetime] = None

# Hold references to fire-and-forget cache tasks so they aren't GC'd mid-run.
_cache_bg_tasks: set = set()

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
    "interior_sunroof",
    "interior_detail",
    "other",
]

# Exterior full-car angles, most-hero first. Used by lead_with_hero().
_EXTERIOR_ANGLES = [
    "exterior_front_left", "exterior_front_right", "exterior_front",
    "exterior_right", "exterior_rear_right", "exterior_rear",
    "exterior_rear_left", "exterior_left",
]


def lead_with_hero(exterior_photos: list) -> list:
    """
    Reorder a list of exterior photo dicts so the HERO (first) matches the common
    dealer convention: a front 3/4 shot — front-LEFT preferred, then front-right,
    then the straight front, then whatever's first. The remaining photos keep
    their walkaround order. (Dealers rarely lead with the straight-on hood shot;
    AG Auto leads with front-left.)
    """
    if not exterior_photos:
        return exterior_photos
    hero = None
    for lbl in ("exterior_front_left", "exterior_front_right", "exterior_front"):
        hero = next((p for p in exterior_photos if p.get("label") == lbl), None)
        if hero:
            break
    if hero is None:
        return exterior_photos
    return [hero] + [p for p in exterior_photos if p is not hero]

# Valid categories Claude can return
VALID_CATEGORIES = set(WALKAROUND_ORDER)

# How many photos to send in a single multi-image API call.
BATCH_SIZE = 5

# Junk URLs are labeled "other" without spending an API call.
_JUNK_URL_PATTERNS = [
    "valuebadge", "showme", "carfax", "autocheck", "logo", "badge",
    "iv.png", "videoplayer", "dealervideopro", "showme.svg",
]


def _is_junk_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _JUNK_URL_PATTERNS)


# ── Multi-image batch classification ──────────────────────────
# One API call classifies BATCH_SIZE downsampled photos at once, returning a
# JSON array of {"index", "category"}. The granular WALKAROUND_ORDER labels are
# preserved so sort_into_walkaround() and photos.py keep working unchanged.
CLASSIFICATION_SYSTEM_PROMPT = """You are classifying car dealership photos for a vehicle walkaround video.
You will see several numbered photos in one request. For EACH numbered photo, assign exactly ONE label.

FULL-CAR EXTERIOR (at least half the car body visible — label by the angle you view the car FROM):
- exterior_front — straight at the FRONT: grille and both headlights centered, car facing you, no side visible
- exterior_front_right — front 3/4 from the RIGHT: you see the front AND the full right (passenger) side together
- exterior_right — the RIGHT side profile: passenger doors face you flat, front points left, front/rear not centered
- exterior_rear_right — rear 3/4 from the RIGHT: you see the rear AND the full right side together
- exterior_rear — straight at the REAR: taillights and bumper centered, back of car faces you, no side visible
- exterior_rear_left — rear 3/4 from the LEFT: you see the rear AND the full left side together
- exterior_left — the LEFT side profile: driver doors face you flat, front points right
- exterior_front_left — front 3/4 from the LEFT: you see the front AND the full left (driver) side together

CLOSE-UP EXTERIOR DETAIL (zoomed on one exterior part, most of the body NOT visible):
- exterior_detail — wheel, tire, headlight, taillight, mirror, badge/emblem, grille close-up, trim piece

INTERIOR:
- interior_dashboard — steering wheel, gauges, infotainment screen, center dash
- interior_seats — seats, headrests, upholstery, rows of seating
- interior_cargo — trunk or cargo/boot area, seats folded for cargo
- interior_sunroof — sunroof / moonroof: a glass roof panel (open or closed), shot from inside looking up or of the roof glass
- interior_detail — center console, door panel, buttons/controls, gear shifter, any other interior close-up

NOT A USABLE CAR PHOTO:
- other — window sticker, price sheet, dealership logo/sign, QR code, carfax report, engine bay, blurry/unclear

Disambiguation rules:
- 3/4 vs straight: if you can see the front AND a full side at once it is *_front_right / *_front_left (rear equivalents for the back) — NOT plain exterior_front / exterior_rear.
- side profile vs 3/4: exterior_left / exterior_right show a flat side with the front/rear NOT centered; if a corner is centered it is a 3/4 view.
- LEFT = driver side, RIGHT = passenger side. Judge by which side of the car faces the camera.
- If most of the body is NOT visible and it is a zoomed exterior part → exterior_detail (never a full exterior angle).
- When unsure between interior_detail and other, choose interior_detail.
- A sunroof / moonroof (glass roof panel, open or closed) is a sought-after feature → interior_sunroof, NOT exterior_detail / interior_detail / other.

Return ONLY a JSON array, one object per photo IN ORDER, no markdown, no prose:
[{"index": 1, "category": "exterior_front"}, {"index": 2, "category": "interior_seats"}]
Every photo must get exactly one category from the labels above."""


def _parse_classification_json(raw: str) -> list:
    """Strip any markdown fences and parse the model's JSON array response."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


async def _classify_batch_anthropic(images_b64: list, model: str):
    content = [{"type": "text", "text": f"Classify these {len(images_b64)} photos:"}]
    for i, img_b64 in enumerate(images_b64, start=1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })

    response = await _client.messages.create(
        model=model,
        max_tokens=1000,
        system=CLASSIFICATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
    }
    return _parse_classification_json(raw), usage


async def _classify_batch_openai(images_b64: list, model: str):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    content = [{"type": "text", "text": f"Classify these {len(images_b64)} photos:"}]
    for i, img_b64 in enumerate(images_b64, start=1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
        })

    response = await client.chat.completions.create(
        model=model,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    raw = response.choices[0].message.content
    usage = {
        "input_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
    }
    return _parse_classification_json(raw), usage


async def _classify_batch_google(images_b64: list, model: str):
    import google.generativeai as genai

    genai.configure(api_key=settings.google_api_key)
    model_instance = genai.GenerativeModel(
        model, system_instruction=CLASSIFICATION_SYSTEM_PROMPT
    )
    parts = [f"Classify these {len(images_b64)} photos:"]
    for i, img_b64 in enumerate(images_b64, start=1):
        parts.append(f"Photo {i}:")
        parts.append({"mime_type": "image/jpeg", "data": base64.b64decode(img_b64)})

    response = await model_instance.generate_content_async(parts)
    meta = getattr(response, "usage_metadata", None)
    usage = {
        "input_tokens": getattr(meta, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(meta, "candidates_token_count", 0) or 0,
    }
    return _parse_classification_json(response.text), usage


async def classify_photo_batch_via_api(images_b64: list):
    """
    Dispatch a batch of downsampled base64 JPEGs to the configured provider.
    Returns (classifications, usage) where classifications is a list of
    {"index": int, "category": str} and usage is {"input_tokens", "output_tokens"}.
    """
    provider = settings.photo_classifier_provider
    model = settings.photo_classifier_model

    if provider == "anthropic":
        return await _classify_batch_anthropic(images_b64, model)
    elif provider == "openai":
        return await _classify_batch_openai(images_b64, model)
    elif provider == "google":
        return await _classify_batch_google(images_b64, model)
    else:
        raise ValueError(f"Unknown photo_classifier_provider: {provider}")


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
            model="claude-sonnet-4-6",
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
                            "interior_dashboard, interior_seats, interior_cargo, interior_sunroof, interior_detail\n\n"
                            "NOT A CAR PHOTO:\n"
                            "other\n\n"
                            "Rules:\n"
                            "- exterior_* (not exterior_detail) = you can see at least half the car body\n"
                            "- exterior_detail = close-up of wheel, tire, mirror, light, badge, trim piece\n"
                            "- interior_dashboard = steering wheel area, infotainment, gauges\n"
                            "- interior_seats = seats, headrests, upholstery\n"
                            "- interior_cargo = trunk, cargo area\n"
                            "- interior_sunroof = sunroof / moonroof glass roof panel (open or closed)\n"
                            "- interior_detail = console, door panel, controls, buttons, any other interior\n"
                            "- other = logos, dealership signs, window stickers, price sheets, QR codes\n"
                            "- When in doubt between interior_detail and other, choose interior_detail\n"
                            "- A sunroof/moonroof → interior_sunroof (not interior_detail/exterior_detail/other)\n\n"
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


# ── Fire-and-forget cache persist + purge ─────────────────────
async def _persist_and_purge_cache(entries: list) -> None:
    """
    Write new classifications to the cache in ONE bulk insert (ON CONFLICT DO
    NOTHING, so concurrent writers / repeats are safe), then opportunistically
    purge entries older than CACHE_TTL — throttled to once per PURGE_INTERVAL.

    Runs in a background task: never blocks the classify response, never raises.
    """
    global _last_cache_purge_at
    from app.core.database import AsyncSessionLocal
    from app.models.photo_classification_cache import (
        PhotoClassificationCache,
        hash_photo_url,
    )

    # Dialect-appropriate INSERT ... ON CONFLICT DO NOTHING.
    if "postgresql" in settings.database_url:
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert

    try:
        now = datetime.utcnow()

        # Dedup by url_hash (same photo can appear twice with different query strings).
        deduped: dict = {}
        for url, category in entries:
            deduped[hash_photo_url(url)] = (url, category)
        rows = [
            {"url_hash": h, "photo_url": url[:1000], "category": cat, "created_at": now}
            for h, (url, cat) in deduped.items()
        ]

        async with AsyncSessionLocal() as session:
            if rows:
                stmt = (
                    _insert(PhotoClassificationCache)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["url_hash"])
                )
                await session.execute(stmt)
                await session.commit()

            # Throttled auto-purge of stale rows (>2 months old).
            if _last_cache_purge_at is None or (now - _last_cache_purge_at) >= PURGE_INTERVAL:
                _last_cache_purge_at = now
                await session.execute(
                    delete(PhotoClassificationCache).where(
                        PhotoClassificationCache.created_at < now - CACHE_TTL
                    )
                )
                await session.commit()
    except Exception as e:
        print(f"[classify] cache persist/purge failed (non-fatal): {e}")


# ── Batch classification ──────────────────────────────────────
async def classify_photos_batch(
    photo_urls: list[str],
    max_photos: int = 30,
    concurrency: int = 3,
    user_id: Optional[int] = None,
) -> list[dict]:
    """
    Classify multiple photos and return them with granular walkaround labels.

    Downsamples every photo (Part 1) then classifies them in multi-image batches
    of BATCH_SIZE — one API call per batch instead of one per photo — through the
    provider/model configured in settings (Part 2). Junk URLs and photos that
    fail to download are labeled "other" without spending an API call.

    Results are cached by photo URL (Part 4): photos already classified in a
    prior import are served from `photo_classification_cache` and never re-sent
    to the model. Only genuine API classifications are cached — junk URLs, failed
    downloads, and batch errors fall back to "other" but are NOT cached (so a
    transient failure can't poison the cache permanently).

    Real per-provider token usage is aggregated and logged to api_usage with the
    actual model string, so model/cost comparisons are exact.

    Args:
        photo_urls:  List of photo URLs to classify
        max_photos:  Maximum number to classify (saves cost)
        concurrency: How many batches to classify simultaneously
        user_id:     For usage attribution in api_usage (optional)

    Returns:
        List of dicts in original order: [{"url": "...", "label": "exterior_front"}, ...]
    """
    from app.core.database import AsyncSessionLocal
    from app.models.photo_classification_cache import (
        PhotoClassificationCache,
        hash_photo_url,
    )
    from sqlmodel import select

    urls_to_classify = photo_urls[:max_photos]
    if not urls_to_classify:
        return []
    labels: dict = {}

    # ── Cache lookup first (one query) ─────────────────────────
    hashes = [hash_photo_url(url) for url in urls_to_classify]
    cached: dict = {}
    try:
        async with AsyncSessionLocal() as session:
            result = await session.exec(
                select(PhotoClassificationCache).where(
                    PhotoClassificationCache.url_hash.in_(hashes)
                )
            )
            for row in result.all():
                cached[row.url_hash] = row.category
    except Exception as e:
        print(f"[classify] cache lookup failed (non-fatal): {e}")

    uncached = []
    for url, h in zip(urls_to_classify, hashes):
        if h in cached:
            labels[url] = cached[h]
        else:
            uncached.append(url)

    if not uncached:
        print(f"Classified {len(urls_to_classify)} photos — all {len(urls_to_classify)} served from cache")
        return [{"url": url, "label": labels.get(url, "other")} for url in urls_to_classify]

    # Pre-filter known junk URLs → "other", no API call, not cached.
    to_process = []
    for url in uncached:
        if _is_junk_url(url):
            labels[url] = "other"
        else:
            to_process.append(url)

    # Downsample everything first (concurrent). Failed fetches → "other", not cached.
    downsampled = await asyncio.gather(
        *[fetch_and_downsample(url) for url in to_process]
    )
    valid_pairs = []
    for url, b64 in zip(to_process, downsampled):
        if b64:
            valid_pairs.append((url, b64))
        else:
            labels[url] = "other"

    # Batch into groups of BATCH_SIZE — one API call per batch.
    batches = [
        valid_pairs[i:i + BATCH_SIZE]
        for i in range(0, len(valid_pairs), BATCH_SIZE)
    ]
    sem = asyncio.Semaphore(concurrency)

    async def run_batch(batch: list):
        batch_urls = [p[0] for p in batch]
        batch_b64s = [p[1] for p in batch]
        out: dict = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        async with sem:
            try:
                classifications, usage = await classify_photo_batch_via_api(batch_b64s)
            except Exception as e:
                print(f"Batch classification failed: {e}")
                # Don't lose the photos — mark "other", but NOT cacheable (transient).
                return {url: "other" for url in batch_urls}, usage, False

        for item in classifications:
            idx = item.get("index")
            category = item.get("category", "other")
            if category not in VALID_CATEGORIES:
                category = "other"
            if isinstance(idx, int) and 1 <= idx <= len(batch_urls):
                out[batch_urls[idx - 1]] = category

        # Any photo the model skipped → other.
        for url in batch_urls:
            out.setdefault(url, "other")
        return out, usage, True

    batch_results = await asyncio.gather(*[run_batch(b) for b in batches])
    total_in = 0
    total_out = 0
    to_cache = []  # (url, category) — genuine API results only
    for out, usage, cacheable in batch_results:
        labels.update(out)
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)
        if cacheable:
            to_cache.extend(out.items())

    provider = settings.photo_classifier_provider
    model = settings.photo_classifier_model
    print(
        f"Classified {len(urls_to_classify)} photos "
        f"({len(cached)} from cache, {len(valid_pairs)} classified in "
        f"{len(batches)} batch call(s) of up to {BATCH_SIZE}, "
        f"provider={provider}, model={model}) — tokens in={total_in} out={total_out}"
    )

    # Log real usage (correct model + real tokens). quantity = photos actually
    # sent to the API this call (cache hits are free). Skip if nothing hit the API.
    if batches:
        from app.services.analytics import record_api_usage
        await record_api_usage(
            "photo_classification",
            user_id=user_id,
            quantity=len(valid_pairs),
            input_tokens=total_in,
            output_tokens=total_out,
            model=model,
        )

    # ── Persist to cache + purge stale rows, fire-and-forget ──
    # Scheduled as a background task so the user NEVER waits on cache writes
    # (bulk ON CONFLICT DO NOTHING insert + throttled >2-month purge).
    if to_cache:
        task = asyncio.create_task(_persist_and_purge_cache(to_cache))
        _cache_bg_tasks.add(task)
        task.add_done_callback(_cache_bg_tasks.discard)

    # Preserve original order and the {"url","label"} contract.
    return [{"url": url, "label": labels.get(url, "other")} for url in urls_to_classify]


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
        "interior_cargo", "interior_sunroof", "interior_detail",
    }

    exterior_photos = lead_with_hero([
        p for p in sorted_photos if p["label"] in exterior_categories
    ])
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