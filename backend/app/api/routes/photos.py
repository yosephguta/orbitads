from __future__ import annotations

"""
Photos Route
─────────────
Endpoint for classifying car photos by angle/position.
Called by the Chrome extension after scraping a listing.
Returns classified photos so the user can review before generating.
"""

import asyncio
import uuid

from typing import Annotated
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import get_settings
from app.core.security import get_current_user
from app.core.middleware import require_active_subscription
from app.core.database import get_session
from app.models.user import User
from app.services import s3 as s3_service
from app.services.photo_classifier import classify_photos_batch, sort_into_walkaround, lead_with_hero

router = APIRouter(
    prefix="/photos", 
    tags=["photos"], 
    dependencies=[Depends(require_active_subscription)],
    )


class ClassifyRequest(BaseModel):
    photo_urls: list[str]


class ClassifiedPhoto(BaseModel):
    url:   str
    label: str


class ClassifyResponse(BaseModel):
    classified: list[ClassifiedPhoto]
    exterior:   list[str]
    interior:   list[str]
    additional: list[str]
    other:      list[str]


@router.post("/classify", response_model=ClassifyResponse)
async def classify_photos(
    payload: ClassifyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Classify car photos by angle and return them grouped and sorted.

    The extension calls this after scraping a listing.
    The response feeds the photo review UI in the popup.
    """
    # Trial users who've used all 5 free videos can't import/classify — this
    # calls Claude and costs API credits. (require_active_subscription already
    # blocks trial-expired / cancelled / past_due / dealership-inactive; this
    # covers the video-limit case, which is not is_blocked.) Uses the EFFECTIVE
    # status so dealership team members (not trial) aren't limited.
    from app.services.entitlement import resolve_entitlement
    _ent = await resolve_entitlement(current_user, session)
    if _ent["status"] == "trial" and (current_user.trial_video_count or 0) >= 5:
        raise HTTPException(status_code=403, detail="TRIAL_VIDEO_LIMIT")

    # Limit to 30 photos max to control cost
    photos_to_classify = payload.photo_urls[:30]

    # Classify photos in downsampled multi-image batches — must match the
    # frontend cap (30) so exterior photos at positions 20-29 aren't dropped.
    # Usage (real model + token counts) is logged inside classify_photos_batch.
    classified = await classify_photos_batch(
        photo_urls=photos_to_classify,
        max_photos=30,
        concurrency=3,
        user_id=current_user.id,
    )

    # Sort into walkaround order
    sorted_photos = sort_into_walkaround(classified)

    # Split into groups for the UI
    exterior_labels = {
    "exterior_front", "exterior_front_right", "exterior_right",
    "exterior_rear_right", "exterior_rear", "exterior_rear_left",
    "exterior_left", "exterior_front_left",
    # exterior_detail intentionally excluded — goes to additional
    }
    interior_labels = {
        "interior_dashboard", "interior_seats", "interior_cargo",
        # A sunroof/moonroof is a sought-after INTERIOR feature — surface it in the
        # interior group. (Other interior close-ups stay in "additional".)
        "interior_sunroof",
    }
    additional_labels = {
        "interior_detail",   # console, door panel, controls, buttons, gear shifter
        "exterior_detail",   # wheels, lights, badges, trim close-ups
    }

    # Exterior leads with the dealer-convention hero (front 3/4), not the straight
    # hood shot — lead_with_hero reorders it; the rest keep walkaround order.
    exterior_dicts = lead_with_hero([p for p in sorted_photos if p["label"] in exterior_labels])
    exterior   = [p["url"] for p in exterior_dicts]
    interior   = [p["url"] for p in sorted_photos if p["label"] in interior_labels]
    additional = [p["url"] for p in sorted_photos if p["label"] in additional_labels]
    other      = [p["url"] for p in sorted_photos if p["label"] == "other"]

    return ClassifyResponse(
        classified=[ClassifiedPhoto(**p) for p in classified],
        exterior=exterior,
        interior=interior,
        additional=additional,
        other=other,
    )


@router.post("/upload")
async def upload_user_photo(
    current_user: Annotated[User, Depends(get_current_user)],
    file: UploadFile = File(...),
):
    """
    Upload a photo from local disk to S3 and return its public URL.
    Called when the user adds a custom photo in the photo review screen.
    The returned URL is stored in reviewPhotos so it survives popup close/reopen
    and is accessible by Shotstack during video generation.
    """
    content_type = file.content_type or "image/jpeg"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    orig = file.filename or "photo.jpg"
    ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        ext = "jpg"

    data = await file.read()
    s3_key = f"public/user_photos/{current_user.id}/{uuid.uuid4().hex}.{ext}"

    await asyncio.to_thread(s3_service.upload_bytes, data, s3_key, content_type)

    settings = get_settings()
    public_url = (
        f"https://{s3_service.BUCKET}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    )
    return {"url": public_url}