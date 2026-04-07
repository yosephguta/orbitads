from __future__ import annotations

"""
Photos Route
─────────────
Endpoint for classifying car photos by angle/position.
Called by the Chrome extension after scraping a listing.
Returns classified photos so the user can review before generating.
"""

from typing import Annotated
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.security import get_current_user
from app.models.user import User
from app.services.photo_classifier import classify_photos_batch, sort_into_walkaround

router = APIRouter(prefix="/photos", tags=["photos"])


class ClassifyRequest(BaseModel):
    photo_urls: list[str]


class ClassifiedPhoto(BaseModel):
    url:   str
    label: str


class ClassifyResponse(BaseModel):
    classified: list[ClassifiedPhoto]
    exterior:   list[str]   # URLs in walkaround order
    interior:   list[str]   # Interior URLs in order
    other:      list[str]   # Unclassified URLs


@router.post("/classify", response_model=ClassifyResponse)
async def classify_photos(
    payload: ClassifyRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Classify car photos by angle and return them grouped and sorted.

    The extension calls this after scraping a listing.
    The response feeds the photo review UI in the popup.
    """
    # Limit to 20 photos max to control cost
    photos_to_classify = payload.photo_urls[:20]

    # Classify all photos concurrently
    classified = await classify_photos_batch(
        photo_urls=photos_to_classify,
        max_photos=20,
        concurrency=3,
    )

    # Sort into walkaround order
    sorted_photos = sort_into_walkaround(classified)

    # Split into groups for the UI
    exterior_labels = {
        "exterior_front", "exterior_front_right", "exterior_right",
        "exterior_rear_right", "exterior_rear", "exterior_rear_left",
        "exterior_left", "exterior_front_left",
    }
    interior_labels = {
        "interior_dashboard", "interior_seats",
        "interior_cargo", "interior_detail",
    }

    exterior = [p["url"] for p in sorted_photos if p["label"] in exterior_labels]
    interior = [p["url"] for p in sorted_photos if p["label"] in interior_labels]
    other    = [p["url"] for p in sorted_photos if p["label"] == "other"]

    return ClassifyResponse(
        classified=[ClassifiedPhoto(**p) for p in classified],
        exterior=exterior,
        interior=interior,
        other=other,
    )