from __future__ import annotations

"""
HeyGen Avatar Service
──────────────────────
Generates a talking avatar video from:
  - avatar_id: the salesperson's photo avatar (created once in HeyGen dashboard)
  - audio_url: the ElevenLabs generated audio file (publicly accessible URL)
  - script:    the ad script text (used as fallback if no audio URL)

HeyGen video generation is async — you submit a job and poll for completion.

Flow:
  1. submit_video()     → returns video_id immediately
  2. get_video_status() → poll until status is "completed" or "failed"
  3. Download the video URL and save to S3
"""

import asyncio
import httpx

from app.core.config import get_settings

settings = get_settings()

BASE_URL = "https://api.heygen.com"


def _headers() -> dict:
    """Auth headers for every HeyGen request."""
    return {
        "X-Api-Key": settings.heygen_api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── Submit video generation job ───────────────────────────────
async def submit_video(
    avatar_id: str,
    audio_url: str,
    script_text: str = "",
    width: int = 1280,
    height: int = 720,
) -> str:
    """
    Submit a video generation job to HeyGen.
    Returns a video_id — use this to poll for completion.

    Args:
        avatar_id:   The salesperson's photo avatar ID from HeyGen
        audio_url:   Public URL of the ElevenLabs audio file
                     HeyGen downloads this and lip-syncs the avatar to it
        script_text: Fallback text if audio_url is not provided
        width:       Video width in pixels (default 1280 = 720p)
        height:      Video height in pixels (default 720)

    Returns:
        video_id string — poll get_video_status() until complete
    """
    # Build the video generation payload
    # HeyGen v2 API uses a "clips" array where each clip is one scene
    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": avatar_id,
                },
                "voice": {
                    # Use the pre-generated audio file from ElevenLabs
                    "type": "audio",
                    "audio_url": audio_url,
                },
                "background": {
                    # Clean white background — Shotstack will replace this
                    "type": "color",
                    "value": "#ffffff",
                },
            }
        ],
        "dimension": {
            "width": width,
            "height": height,
        },
        # test=True uses fewer credits during development
        # Set to False in production for full quality
        "test": True,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{BASE_URL}/v2/video/generate",
            headers=_headers(),
            json=payload,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HeyGen video submit failed: {response.status_code} {response.text}"
            )

        data = response.json()

        # HeyGen wraps responses in a "data" key
        video_id = data.get("data", {}).get("video_id")
        if not video_id:
            raise RuntimeError(
                f"HeyGen did not return a video_id. Response: {data}"
            )

        return video_id


# ── Poll video status ─────────────────────────────────────────
async def get_video_status(video_id: str) -> dict:
    """
    Check the status of a HeyGen video generation job.

    Returns a dict with:
        status:    "pending" | "processing" | "completed" | "failed"
        video_url: download URL (only present when status is "completed")
        error:     error message (only present when status is "failed")
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{BASE_URL}/v1/video_status.get",
            headers=_headers(),
            params={"video_id": video_id},
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HeyGen status check failed: {response.status_code} {response.text}"
            )

        data = response.json().get("data", {})

        return {
            "status": data.get("status", "unknown"),
            "video_url": data.get("video_url"),
            "error": data.get("error"),
            "duration": data.get("duration"),
        }


# ── Wait for completion ───────────────────────────────────────
async def wait_for_video(
    video_id: str,
    poll_interval: int = 10,   # check every 10 seconds
    max_wait: int = 600,       # give up after 10 minutes
) -> str:
    """
    Poll HeyGen until the video is ready, then return the download URL.

    Args:
        video_id:      From submit_video()
        poll_interval: Seconds between status checks
        max_wait:      Maximum total seconds to wait before giving up

    Returns:
        video_url — the URL to download the completed video

    Raises:
        RuntimeError if video fails or times out
    """
    elapsed = 0

    while elapsed < max_wait:
        status_data = await get_video_status(video_id)
        status = status_data["status"]

        if status == "completed":
            video_url = status_data.get("video_url")
            if not video_url:
                raise RuntimeError("HeyGen completed but returned no video URL.")
            return video_url

        if status == "failed":
            error = status_data.get("error", "Unknown error")
            raise RuntimeError(f"HeyGen video generation failed: {error}")

        # Still processing — wait and try again
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise RuntimeError(
        f"HeyGen video timed out after {max_wait} seconds. "
        f"video_id: {video_id}"
    )


# ── Download video bytes ──────────────────────────────────────
async def download_video(video_url: str) -> bytes:
    """
    Download the completed video from HeyGen's CDN.
    Returns raw MP4 bytes — save these to S3.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(video_url)

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to download HeyGen video: {response.status_code}"
            )

        return response.content