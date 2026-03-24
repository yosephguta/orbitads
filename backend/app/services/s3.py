from __future__ import annotations

"""
S3 Service
───────────
Handles all file storage for OrbitAds.

Files stored:
  uploads/{user_id}/photos/{uuid}.jpg   — salesperson photos
  uploads/{user_id}/voice/{uuid}.mp3    — voice recording
  outputs/{job_id}/audio.mp3            — ElevenLabs generated audio
  outputs/{job_id}/avatar.mp4           — HeyGen avatar video
  outputs/{job_id}/final_ad.mp4         — Shotstack final video

Key design: the browser uploads directly to S3 using presigned URLs.
The API server never touches the file bytes — only generates the URLs.
This keeps the API fast and avoids large file transfers through our server.
"""

import uuid
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

settings = get_settings()

# ── S3 client ─────────────────────────────────────────────────
# Created once and reused. boto3 clients are thread-safe.
_s3 = boto3.client(
    "s3",
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
    region_name=settings.aws_region,
)

BUCKET = settings.s3_bucket_name


# ── Key generators ────────────────────────────────────────────
# S3 keys are like file paths inside the bucket.
# We use UUIDs to avoid collisions between users.

def make_photo_key(user_id: int, filename: str) -> str:
    """e.g. uploads/42/photos/a1b2c3d4.jpg"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"uploads/{user_id}/photos/{uuid.uuid4().hex}.{ext}"


def make_voice_key(user_id: int, filename: str) -> str:
    """e.g. uploads/42/voice/a1b2c3d4.mp3"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3"
    return f"uploads/{user_id}/voice/{uuid.uuid4().hex}.{ext}"


def make_audio_output_key(job_id: int) -> str:
    """e.g. outputs/99/audio.mp3 — ElevenLabs generated audio"""
    return f"outputs/{job_id}/audio.mp3"


def make_avatar_output_key(job_id: int) -> str:
    """e.g. outputs/99/avatar.mp4 — HeyGen avatar video"""
    return f"outputs/{job_id}/avatar.mp4"


def make_final_video_key(job_id: int) -> str:
    """e.g. outputs/99/final_ad.mp4 — Shotstack assembled video"""
    return f"outputs/{job_id}/final_ad.mp4"


# ── Presigned upload URL ───────────────────────────────────────
def create_presigned_upload_url(
    s3_key: str,
    content_type: str,
    expires_in: int = 300,  # 5 minutes
) -> dict:
    """
    Generate a presigned POST URL for direct browser → S3 upload.

    Returns a dict with two keys:
      url:    the S3 endpoint to POST to
      fields: form fields to include alongside the file

    How the frontend uses this:
        const { url, fields } = response.data
        const form = new FormData()
        Object.entries(fields).forEach(([k, v]) => form.append(k, v))
        form.append('file', file)           ← file must be last
        await axios.post(url, form)

    The 50MB limit prevents accidental huge uploads during development.
    Raise it later for production video uploads if needed.
    """
    try:
        presigned = _s3.generate_presigned_post(
            Bucket=BUCKET,
            Key=s3_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, 52_428_800],  # 1 byte to 50MB
            ],
            ExpiresIn=expires_in,
        )
        return presigned  # {"url": "...", "fields": {...}}
    except ClientError as e:
        raise RuntimeError(f"Could not generate upload URL: {e}") from e


# ── Presigned download URL ─────────────────────────────────────
def create_presigned_download_url(
    s3_key: str,
    expires_in: int = 3600,  # 1 hour
) -> str:
    """
    Generate a presigned GET URL so a browser can download/stream a file.
    Used to serve the final video to the frontend without making it public.
    """
    try:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": s3_key},
            ExpiresIn=expires_in,
        )
    except ClientError as e:
        raise RuntimeError(f"Could not generate download URL: {e}") from e


# ── Server-side upload ─────────────────────────────────────────
def upload_bytes(data: bytes, s3_key: str, content_type: str) -> str:
    """
    Upload raw bytes directly to S3 from the server.
    Used by Celery workers to save ElevenLabs audio and HeyGen video.
    Returns the s3_key so callers can save it to the database.
    """
    try:
        _s3.put_object(
            Bucket=BUCKET,
            Key=s3_key,
            Body=data,
            ContentType=content_type,
        )
        return s3_key
    except ClientError as e:
        raise RuntimeError(f"Could not upload to S3: {e}") from e


# ── Download bytes ─────────────────────────────────────────────
def download_bytes(s3_key: str) -> bytes:
    """
    Download a file from S3 and return its raw bytes.
    Used by workers that need to process a file (e.g. sending voice
    recording bytes to ElevenLabs for cloning).
    """
    try:
        response = _s3.get_object(Bucket=BUCKET, Key=s3_key)
        return response["Body"].read()
    except ClientError as e:
        raise RuntimeError(f"Could not download from S3: {e}") from e


# ── Delete ─────────────────────────────────────────────────────
def delete_object(s3_key: str) -> None:
    """Delete a single file. Used for cleanup after jobs complete."""
    try:
        _s3.delete_object(Bucket=BUCKET, Key=s3_key)
    except ClientError as e:
        raise RuntimeError(f"Could not delete from S3: {e}") from e
    

def get_audio_duration(s3_key: str) -> float:
    """
    Download audio from S3 and return its duration in seconds.
    Uses mutagen to read MP3 metadata without loading the full file.
    """
    import tempfile
    import os
    from mutagen.mp3 import MP3

    # Download to a temp file — mutagen needs a file path
    audio_bytes = download_bytes(s3_key)

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        audio = MP3(tmp_path)
        return round(audio.info.length, 2)
    finally:
        os.unlink(tmp_path)