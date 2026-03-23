from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import SQLModel

from app.core.security import get_current_user
from app.models.user import User
from app.services import s3

router = APIRouter(prefix="/uploads", tags=["uploads"])

# ── Allowed file types ────────────────────────────────────────
# We validate content types so users can't upload unexpected files.
ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}

ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",       # .mp3
    "audio/mp4",        # .m4a
    "audio/wav",        # .wav
    "audio/webm",       # .webm (browser recording format)
    "audio/ogg",        # .ogg
    "audio/x-wav",      # some browsers send this for .wav
}


# ── Request / Response shapes ─────────────────────────────────
class UploadRequest(SQLModel):
    filename: str
    content_type: str


class UploadResponse(SQLModel):
    s3_key: str       # save this and pass it to POST /jobs
    upload_url: str   # POST the file to this URL
    upload_fields: dict  # include these fields in the form data


# ── Photo upload ──────────────────────────────────────────────
@router.post("/photo", response_model=UploadResponse)
async def request_photo_upload(
    payload: UploadRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Get a presigned S3 URL to upload a salesperson photo.

    The frontend calls this once per photo (up to 5 photos).
    Each call returns a unique S3 key — save all of them as a JSON array
    and pass them to POST /jobs as photos_s3_keys.

    Example flow:
        POST /uploads/photo  →  { s3_key, upload_url, upload_fields }
        PUT upload_url (with file)  →  file is now in S3
        POST /jobs  →  { photos_s3_keys: '["uploads/1/photos/abc.jpg"]' }
    """
    if payload.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported image type '{payload.content_type}'. "
                   f"Allowed: jpeg, png, webp.",
        )

    key = s3.make_photo_key(current_user.id, payload.filename)
    presigned = s3.create_presigned_upload_url(key, payload.content_type)

    return UploadResponse(
        s3_key=key,
        upload_url=presigned["url"],
        upload_fields=presigned["fields"],
    )


# ── Voice upload ──────────────────────────────────────────────
@router.post("/voice", response_model=UploadResponse)
async def request_voice_upload(
    payload: UploadRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Get a presigned S3 URL to upload a 60-second voice recording.

    The recording is used by ElevenLabs to clone the salesperson's voice.
    Better quality recording = better voice clone.

    Tips for a good recording:
      - Quiet room, no background noise
      - Speak clearly and naturally for 60 seconds
      - Read anything — a news article, a script, anything
    """
    if payload.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported audio type '{payload.content_type}'. "
                   f"Allowed: mp3, mp4, wav, webm, ogg.",
        )

    key = s3.make_voice_key(current_user.id, payload.filename)
    presigned = s3.create_presigned_upload_url(key, payload.content_type)

    return UploadResponse(
        s3_key=key,
        upload_url=presigned["url"],
        upload_fields=presigned["fields"],
    )