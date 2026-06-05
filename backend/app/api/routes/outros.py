from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.outro_video import OutroVideo, OutroVideoRead
from app.models.user import User
from app.services import s3

router = APIRouter(prefix="/outros", tags=["outros"])

ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",  # .mov
    "video/webm",
    "video/x-m4v",
}


@router.post("/upload", response_model=OutroVideoRead, status_code=status.HTTP_201_CREATED)
async def upload_outro(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: UploadFile = File(...),
    name: str = Form(...),
):
    """
    Upload a user-recorded outro clip. Stored in S3, record saved to DB.
    Returns the outro with a fresh 7-day presigned URL.
    """
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported video type '{file.content_type}'. Allowed: mp4, mov, webm.",
        )

    ext = (file.filename or "outro.mp4").rsplit(".", 1)[-1].lower() or "mp4"
    s3_key = s3.make_outro_key(current_user.id, int(time.time()), ext)

    data = await file.read()
    s3.upload_bytes(data, s3_key, file.content_type or "video/mp4")

    outro = OutroVideo(
        user_id=current_user.id,
        name=name.strip(),
        s3_key=s3_key,
        duration_seconds=None,
    )
    session.add(outro)
    await session.commit()
    await session.refresh(outro)

    url = s3.create_presigned_download_url(outro.s3_key, expires_in=604800)
    return OutroVideoRead(**outro.model_dump(), url=url)


@router.get("/", response_model=list[OutroVideoRead])
async def list_outros(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Return all of the current user's outro videos with fresh presigned URLs."""
    result = await session.exec(
        select(OutroVideo)
        .where(OutroVideo.user_id == current_user.id)
        .order_by(OutroVideo.created_at.desc())
    )
    outros = result.all()

    return [
        OutroVideoRead(**o.model_dump(), url=s3.create_presigned_download_url(o.s3_key, expires_in=604800))
        for o in outros
    ]


@router.delete("/{outro_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_outro(
    outro_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Delete an outro video from S3 and the database."""
    outro = await session.get(OutroVideo, outro_id)
    if not outro or outro.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outro not found.")

    try:
        s3.delete_object(outro.s3_key)
    except Exception:
        pass  # S3 failure shouldn't block DB cleanup

    await session.delete(outro)
    await session.commit()
