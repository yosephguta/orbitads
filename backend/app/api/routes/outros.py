from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Annotated, Optional

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


async def _compress_video(data: bytes, in_ext: str) -> bytes:
    """
    Transcode to H.264 MP4, capping height at 1080p, ~2-4 Mbps.
    Runs ffmpeg as a subprocess so the event loop stays free.
    Falls back to raw bytes on any error (ffmpeg missing, encode failure, etc.)
    so an upload never crashes because of compression.
    """
    tmp_in = tmp_out = None
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        with tempfile.NamedTemporaryFile(suffix=f".{in_ext}", delete=False) as f:
            f.write(data)
            tmp_in = f.name
        tmp_out = tmp_in.rsplit(".", 1)[0] + "_out.mp4"

        proc = await asyncio.create_subprocess_exec(
            ffmpeg_exe, "-y",
            "-i", tmp_in,
            # Scale height to 1080 if taller, otherwise keep original size.
            # trunc(...) rounds width down to the nearest even number (required by libx264).
            "-vf", "scale='if(gt(ih,1080),trunc(iw*1080/ih/2)*2,iw)':'if(gt(ih,1080),1080,ih)'",
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-maxrate", "4M", "-bufsize", "8M",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            tmp_out,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            print(f"[outro] ffmpeg failed (rc={proc.returncode}): {stderr.decode()[-500:]}")
            return data

        with open(tmp_out, "rb") as f:
            compressed = f.read()

        print(f"[outro] compressed {len(data) // 1024}KB → {len(compressed) // 1024}KB")
        return compressed

    except FileNotFoundError:
        print("[outro] ffmpeg not found — uploading raw video")
        return data
    except Exception as exc:
        print(f"[outro] compression error ({exc}) — uploading raw video")
        return data
    finally:
        for path in (tmp_in, tmp_out):
            if path and os.path.exists(path):
                os.unlink(path)


@router.post("/upload", response_model=OutroVideoRead, status_code=status.HTTP_201_CREATED)
async def upload_outro(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: UploadFile = File(...),
    name: str = Form(...),
    duration_seconds: Optional[str] = Form(None),
):
    """
    Upload a user-recorded outro clip. Stored in S3, record saved to DB.
    Returns the outro with a fresh 7-day presigned URL.

    `duration_seconds` is the real clip length measured client-side (the
    browser can read video metadata; the backend can't without ffprobe). It's
    stored so the video pipeline uses the actual length instead of a hardcoded
    fallback that cut outros off.
    """
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported video type '{file.content_type}'. Allowed: mp4, mov, webm.",
        )

    parsed_duration: Optional[float] = None
    if duration_seconds:
        try:
            parsed_duration = float(duration_seconds)
        except (ValueError, TypeError):
            parsed_duration = None

    in_ext = (file.filename or "outro.mp4").rsplit(".", 1)[-1].lower() or "mp4"
    # S3 key always ends in .mp4 — compression outputs MP4 regardless of input format.
    s3_key = s3.make_outro_key(current_user.id, int(time.time()), "mp4")

    data = await file.read()
    compressed = await _compress_video(data, in_ext)
    await asyncio.to_thread(s3.upload_bytes, compressed, s3_key, "video/mp4")

    outro = OutroVideo(
        user_id=current_user.id,
        name=name.strip(),
        s3_key=s3_key,
        duration_seconds=parsed_duration,
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
