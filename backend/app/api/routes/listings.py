from __future__ import annotations

"""
Listings Route
───────────────
Handles:
  1. POST /listings/generate     — generate FB listing description
  2. POST /listings/             — save a completed listing to history
  3. GET  /listings/             — get user's listing history (last 20)
  4. PATCH /listings/{id}/posted — mark listing as posted to Facebook
  5. POST /listings/check-sold   — check if vehicles are still listed
"""

import asyncio
import json
import httpx
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.listing import Listing, ListingRead

import anthropic
from app.core.config import get_settings

router   = APIRouter(prefix="/listings", tags=["listings"])
settings = get_settings()
_client  = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


# ── Request/Response models ───────────────────────────────────
class GenerateRequest(BaseModel):
    year:            Optional[str] = None
    make:            Optional[str] = None
    model:           Optional[str] = None
    trim:            Optional[str] = None
    price:           Optional[str] = None
    mileage:         Optional[str] = None
    vin:             Optional[str] = None
    dealership_name: Optional[str] = None
    listing_url:     Optional[str] = None


class GenerateResponse(BaseModel):
    title:       str
    price:       str
    description: str
    tags:        list[str]


class SaveListingRequest(BaseModel):
    job_id:         Optional[int] = None
    vin:            Optional[str] = None
    year:           Optional[str] = None
    make:           Optional[str] = None
    model:          Optional[str] = None
    trim:           Optional[str] = None
    price:          Optional[str] = None
    mileage:        Optional[str] = None
    listing_url:    Optional[str] = None
    fb_title:       Optional[str] = None
    fb_description: Optional[str] = None
    fb_tags:        Optional[str] = None
    video_s3_key:   Optional[str] = None
    video_url:      Optional[str] = None
    photo_urls:     Optional[str] = None


class SoldCheckRequest(BaseModel):
    listing_ids: list[int]


class UpdateSoldStatusRequest(BaseModel):
    sold_ids:    list[int]
    checked_ids: list[int]


# ── Generate FB listing description ──────────────────────────
@router.post("/generate", response_model=GenerateResponse)
async def generate_listing(
    payload: GenerateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Generate a facebook marketplace listing using Claude."""
    vehicle_info = " ".join(filter(None, [
        payload.year,
        payload.make.title() if payload.make else None,
        payload.model,
        payload.trim,
    ])) or "Vehicle"

    price_clean = payload.price or "Call for price"
    mileage_str = payload.mileage or "Contact for mileage"
    dealer      = payload.dealership_name or current_user.dealership_name or "Our Dealership"

    system_prompt = (
        "You are a car salesperson writing a social media post to showcase a vehicle you have available. "
        "You are the salesperson, not the owner. Describe the car the way a professional would present "
        "inventory to a buyer — focus entirely on the vehicle's features, condition, and value."
    )

    # Phrases that indicate private ownership — never acceptable in the description
    _OWNERSHIP_PHRASES = [
        "i've", "i have ", "i took", "i kept", "i loved", "i maintained", "i babied",
        "i'm selling", "selling my", "my car", "my vehicle", "my personal",
        "time to move", "moving on", "day one", "new chapter", "i no longer",
    ]

    def _has_ownership_language(text: str) -> bool:
        lower = text.lower()
        return any(phrase in lower for phrase in _OWNERSHIP_PHRASES)

    if current_user.phone_number:
        cta_instruction = (
            f"End with: 'DM me or call/text {current_user.phone_number} — "
            f"I\\'d love to help you find your next car!'"
        )
    else:
        cta_instruction = "End with: 'DM me for more info or to schedule a test drive!'"

    def _build_prompt(retry_note: str = "") -> str:
        return f"""Write a social media vehicle post for a car salesperson showcasing a vehicle.
{retry_note}
Vehicle: {vehicle_info}
Price: {price_clean}
Mileage: {mileage_str}
VIN: {payload.vin or 'Available on request'}

FORMAT (keep this social-media style with emojis and bullets):
• Title: max 100 chars — "Year Make Model Trim - $Price", no dealership name
• Opening: 1 engaging sentence about the vehicle (lead with the car name/year, not "I")
• 3-5 bullet points with emojis highlighting key features and specs
• 1-2 sentences on condition/appearance (e.g. "Exterior is clean", "Cabin is in great shape")
• CTA: {cta_instruction}
• Tags: 6-8 relevant hashtags

IMPORTANT: You are presenting this vehicle as a salesperson — you did NOT own or drive it.
Write about what the car offers the buyer. Never write from a private owner's perspective.

Respond with ONLY valid JSON: {{"title": "...", "description": "...", "tags": [...]}}"""

    async def _call_claude(retry_note: str = "") -> dict:
        msg = await _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": _build_prompt(retry_note)}],
        )
        raw = msg.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

    data = await _call_claude()

    # Hard check — if ownership language slipped through, retry once with an explicit note
    if _has_ownership_language(data.get("description", "")):
        data = await _call_claude(
            retry_note=(
                "\nNOTE: Do not use 'I've', 'I have', 'selling my', 'my car', or any language "
                "implying you personally own or have history with this vehicle. "
                "Write about the car's features and condition only.\n"
            )
        )

    return GenerateResponse(
        title=data.get("title", vehicle_info),
        price=price_clean,
        description=data.get("description", ""),
        tags=data.get("tags", []),
    )


# ── Save listing to history ───────────────────────────────────
@router.post("/", response_model=ListingRead, status_code=201)
async def save_listing(
    payload: SaveListingRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Save a completed listing to the user's history."""
    # Get existing listings
    result = await session.exec(
        select(Listing)
        .where(Listing.user_id == current_user.id)
        .order_by(Listing.created_at.desc())
    )
    existing = result.all()

    # Enforce 20-listing limit — delete oldest if over limit
    if len(existing) >= 20:
        oldest = existing[-1]
        await session.delete(oldest)

    listing = Listing(
        user_id=current_user.id,
        job_id=payload.job_id,
        vin=payload.vin,
        year=payload.year,
        make=payload.make,
        model=payload.model,
        trim=payload.trim,
        price=payload.price,
        mileage=payload.mileage,
        listing_url=payload.listing_url,
        fb_title=payload.fb_title,
        fb_description=payload.fb_description,
        fb_tags=payload.fb_tags,
        video_s3_key=payload.video_s3_key,
        video_url=payload.video_url,
        photo_urls=payload.photo_urls,
    )
    session.add(listing)
    await session.commit()
    await session.refresh(listing)
    return listing


# ── Get listing history ───────────────────────────────────────
@router.get("/", response_model=list[ListingRead])
async def get_listings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Get user's listing history, newest first."""
    result = await session.exec(
        select(Listing)
        .where(Listing.user_id == current_user.id)
        .order_by(Listing.created_at.desc())
        .limit(20)
    )
    return result.all()


# ── Mark as posted to Facebook ────────────────────────────────
@router.patch("/{listing_id}/posted")
async def mark_posted(
    listing_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Mark a listing as posted to Facebook Marketplace."""
    listing = await session.get(Listing, listing_id)
    if not listing or listing.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Listing not found")

    listing.fb_posted    = True
    listing.fb_posted_at = datetime.now(timezone.utc)
    listing.updated_at   = datetime.now(timezone.utc)
    session.add(listing)
    await session.commit()
    return {"success": True}

class FbPostCaptionRequest(BaseModel):
    year:    Optional[str] = None
    make:    Optional[str] = None
    model:   Optional[str] = None
    trim:    Optional[str] = None
    price:   Optional[str] = None
    mileage: Optional[str] = None
    theme:   str = "hype"


class FbPostCaptionResponse(BaseModel):
    caption: str


_THEME_TONES = {
    "hype":      "High energy, excitement, and urgency. Use lots of emojis. Make it feel like a can't-miss opportunity.",
    "casual":    "Relaxed and conversational. Friendly, low-key, approachable. A few emojis but not over the top.",
    "luxury":    "Sophisticated and premium. Emphasize quality, refinement, and prestige. Minimal but tasteful emojis.",
    "outdoorsy": "Adventure-focused. Emphasize capability, ruggedness, and outdoor lifestyle. Use nature/adventure emojis.",
    "family":    "Warm and family-friendly. Emphasize safety, space, reliability, and practicality.",
}


@router.post("/generate-fb-post-caption", response_model=FbPostCaptionResponse)
async def generate_fb_post_caption(
    payload: FbPostCaptionRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Generate a themed Facebook post caption for a completed job."""
    vehicle_info = " ".join(filter(None, [
        payload.year,
        payload.make.title() if payload.make else None,
        payload.model,
        payload.trim,
    ])) or "Vehicle"

    price_clean  = payload.price or "Call for price"
    mileage_str  = payload.mileage or "N/A"
    tone         = _THEME_TONES.get(payload.theme, _THEME_TONES["hype"])

    if current_user.phone_number:
        cta = f"DM me or call/text {current_user.phone_number} to schedule a test drive!"
    else:
        cta = "DM me for more info or to schedule a test drive!"

    prompt = f"""Write a short Facebook Marketplace post for a car salesperson showcasing this vehicle.

Vehicle: {vehicle_info}
Price: {price_clean}
Mileage: {mileage_str}

Tone: {tone}

Requirements:
- 3-6 lines, social-media style
- Include 2-4 key selling points with emojis
- End with this exact CTA: "{cta}"
- No title line — body text only
- Never use ownership language (e.g. "I've owned", "my car", "selling my")
- You are a salesperson presenting dealership inventory, not a private seller

Return ONLY the caption text. No JSON, no quotes, no extra formatting."""

    msg = await _client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return FbPostCaptionResponse(caption=msg.content[0].text.strip())


class ScriptRequest(BaseModel):
    vehicle_info:  str
    custom_prompt: str

@router.post("/generate-script")
async def generate_custom_script(
    payload: ScriptRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    if current_user.phone_number:
        script_cta = f'End with: "Send me a message or call/text {current_user.phone_number}!"'
    else:
        script_cta = 'End with: "Send me a message today!" or similar personal CTA'

    message = await _client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Write a 30-second video ad script for this car salesperson.

Vehicle: {payload.vehicle_info}

Their style/prompt: {payload.custom_prompt}

Write a natural, conversational script (60-70 words) that captures their style.
{script_cta}
Return ONLY the script text, no labels or formatting."""
        }]
    )
    return {"script": message.content[0].text.strip()}


# ── Check sold status ─────────────────────────────────────────
@router.post("/check-sold")
async def check_sold(
    payload: SoldCheckRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Check if vehicles are still listed on the dealership website.
    Called by the extension daily.
    Returns list of listing IDs that appear to be sold.
    """
    # Domains that block datacenter IPs — must be checked from the extension instead
    EXTENSION_ONLY_DOMAINS = {'jbakia.com', 'www.jbakia.com'}

    sold_ids = []

    async with httpx.AsyncClient(timeout=30) as client:
        for listing_id in payload.listing_ids:
            listing = await session.get(Listing, listing_id)
            if not listing or listing.user_id != current_user.id:
                continue
            if not listing.listing_url:
                continue

            try:
                from urllib.parse import urlparse
                domain = urlparse(listing.listing_url).hostname or ''
                if domain in EXTENSION_ONLY_DOMAINS:
                    print(f'Listing {listing_id} is on {domain} — skipping backend check (use extension)')
                    continue
            except Exception:
                pass

            try:
                resp = await client.get(
                    listing.listing_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Connection': 'keep-alive',
                    },
                    follow_redirects=True,
                )

                # Rate limited — skip, try again later
                if resp.status_code == 429:
                    print(f'Rate limited for listing {listing_id} — skipping')
                    listing.last_checked_at = datetime.now(timezone.utc)
                    await session.commit()
                    continue

                is_sold = False

                # Hard 404 or 410 Gone — confirm with a second request to avoid false positives
                if resp.status_code in (404, 410):
                    await asyncio.sleep(3)
                    confirm = await client.get(
                        listing.listing_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            'Accept': 'text/html,application/xhtml+xml',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Connection': 'keep-alive',
                        },
                        follow_redirects=True,
                    )
                    if confirm.status_code in (404, 410):
                        is_sold = True
                    else:
                        print(f'Listing {listing_id} got {resp.status_code} then {confirm.status_code} on confirm — skipping')
                        listing.last_checked_at = datetime.now(timezone.utc)
                        await session.commit()
                        continue
                elif resp.status_code == 200:
                    text = resp.text

                    # JBA-specific sold indicators
                    # JBA returns 200 with custom alias-404 page when vehicle is gone
                    jba_indicators = [
                        'alias-404',                   # CSS class on html element
                        'goneAliasRedirect',           # JS variable for missing VDP
                        'redirectFromMissingVDP=true', # Query param in redirect URL
                    ]

                    # Generic sold indicators for other dealerships
                    generic_indicators = [
                        'this vehicle has been sold',
                        'vehicle is no longer available',
                        'no longer available',
                        'vehicle sold',
                        'page not found',
                    ]

                    all_indicators = jba_indicators + generic_indicators
                    is_sold = any(indicator in text for indicator in all_indicators)

                    # Also check if redirected away from VDP to inventory search
                    final_url = str(resp.url)
                    original_path = listing.listing_url.split('jbakia.com')[-1] if 'jbakia' in listing.listing_url else ''
                    if original_path and original_path not in final_url and 'inventory' in final_url:
                        is_sold = True
                        print(f'Listing {listing_id} redirected to inventory — marking sold')
                else:
                    # Unknown status — skip
                    print(f'Unexpected status {resp.status_code} — skipping listing {listing_id}')
                    continue

                # Add delay between requests to avoid rate limiting
                await asyncio.sleep(2)

                # Update last checked
                listing.last_checked_at = datetime.now(timezone.utc)
                listing.updated_at      = datetime.now(timezone.utc)

                if is_sold and not listing.is_sold:
                    listing.is_sold          = True
                    listing.sold_detected_at = datetime.now(timezone.utc)
                    sold_ids.append(listing_id)
                    print(f'Listing {listing_id} marked as SOLD')

                session.add(listing)
                await session.commit()

            except Exception as e:
                print(f"Sold check failed for listing {listing_id}: {e}")
                continue

    return {"sold_ids": sold_ids}


# ── Report sold check results from extension ──────────────────
@router.post("/update-sold-status")
async def update_sold_status(
    payload: UpdateSoldStatusRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Called by the extension after it checks listing URLs directly.
    Extension passes which listings were checked and which are confirmed sold.
    """
    now = datetime.now(timezone.utc)
    confirmed_sold = []

    for listing_id in payload.checked_ids:
        listing = await session.get(Listing, listing_id)
        if not listing or listing.user_id != current_user.id:
            continue

        listing.last_checked_at = now
        listing.updated_at      = now

        if listing_id in payload.sold_ids and not listing.is_sold:
            listing.is_sold          = True
            listing.sold_detected_at = now
            confirmed_sold.append(listing_id)
            print(f'Listing {listing_id} marked as SOLD via extension check')

        session.add(listing)

    await session.commit()
    return {"sold_ids": confirmed_sold}