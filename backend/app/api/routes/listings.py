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


# ── Generate FB listing description ──────────────────────────
@router.post("/generate", response_model=GenerateResponse)
async def generate_listing(
    payload: GenerateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Generate a Facebook Marketplace listing using Claude."""
    vehicle_info = " ".join(filter(None, [
        payload.year,
        payload.make.title() if payload.make else None,
        payload.model,
        payload.trim,
    ])) or "Vehicle"

    price_clean = payload.price or "Call for price"
    mileage_str = payload.mileage or "Contact for mileage"
    dealer      = payload.dealership_name or current_user.dealership_name or "Our Dealership"

    prompt = f"""Write a Facebook Marketplace vehicle listing for a dealership salesperson.

Vehicle: {vehicle_info}
Price: {price_clean}
Mileage: {mileage_str}
VIN: {payload.vin or 'Available on request'}
Dealership: {dealer}
{f"Original listing: {payload.listing_url}" if payload.listing_url else ""}

Requirements:
- Title: max 100 chars, format "Year Make Model Trim - $Price - Dealership"
- Description: 200-300 words, conversational tone
  * Opening hook (why this is a great deal)
  * Key features with emojis as bullet points
  * Brief condition note
  * Call to action mentioning the dealership name
  * Mention financing options are available through the dealership
- Tags: 6-8 relevant tags (make, model, year, body style, price range, features)
- Write as if the salesperson is posting personally, not as a business ad
- Keep it genuine and conversational, not overly sales-y

Respond with ONLY valid JSON, no markdown:
{{"title": "...", "description": "...", "tags": ["...", "..."]}}"""

    message = await _client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw  = message.content[0].text.strip()
    raw  = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)

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
    sold_ids = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for listing_id in payload.listing_ids:
            listing = await session.get(Listing, listing_id)
            if not listing or listing.user_id != current_user.id:
                continue
            if not listing.listing_url:
                continue

            try:
                resp = await client.get(
                    listing.listing_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    follow_redirects=True,
                )

                is_sold = False
                if resp.status_code == 404:
                    is_sold = True
                elif resp.status_code == 200:
                    text = resp.text.lower()
                    sold_phrases = [
                        "vehicle not found", "listing not found",
                        "no longer available", "this vehicle has been sold",
                        "vehicle sold", "page not found",
                    ]
                    if any(phrase in text for phrase in sold_phrases):
                        is_sold = True
                    elif listing.vin and listing.vin.lower() not in text:
                        is_sold = True

                listing.last_checked_at = datetime.now(timezone.utc)
                listing.updated_at      = datetime.now(timezone.utc)

                if is_sold and not listing.is_sold:
                    listing.is_sold          = True
                    listing.sold_detected_at = datetime.now(timezone.utc)
                    sold_ids.append(listing_id)

                session.add(listing)

            except Exception as e:
                print(f"Sold check failed for listing {listing_id}: {e}")
                continue

    await session.commit()
    return {"sold_ids": sold_ids}