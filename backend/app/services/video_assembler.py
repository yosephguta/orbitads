from __future__ import annotations

"""
Shotstack Video Assembly Service
──────────────────────────────────
Two ad formats:

1. Slideshow only:
   |---------------------- Photo slideshow (100%) ----------------------|
   |              Car photos with motion effects + text overlays        |
   |<-------------- ElevenLabs audio runs the full length -------------->|

2. Slideshow + Outro:
   |---------- Photo slideshow (20-25s) ----------|--- Outro clip (5-10s) ---|
   |   Car photos + overlays + voiceover audio    |  Raw user-recorded clip  |
   Vehicle name and CTA overlays stop at slideshow end — outro handles the CTA.
"""

import asyncio
from typing import Optional

import httpx

from app.core.config import get_settings

settings = get_settings()

SHOTSTACK_URL        = "https://api.shotstack.io/v1/render"
SHOTSTACK_STATUS_URL = "https://api.shotstack.io/v1/render/{render_id}"

DEFAULT_BRAND_COLOR = "#C4122F"

# Slideshow effects — cinematic but not distracting
SLIDESHOW_EFFECTS = [
    {"effect": "zoomIn",  "transition_in": "fade",     "transition_out": "fadeSlow"},
    {"effect": "zoomOut", "transition_in": "fadeSlow",  "transition_out": "fadeSlow"},
    {"effect": "zoomIn",  "transition_in": "fadeSlow",  "transition_out": "fadeSlow"},
    {"effect": "zoomOut", "transition_in": "fadeSlow",  "transition_out": "fadeSlow"},
    {"effect": "zoomIn",  "transition_in": "fadeSlow",  "transition_out": "fadeSlow"},
    {"effect": "zoomIn",  "transition_in": "fadeSlow",  "transition_out": "fadeSlow"},
    {"effect": "zoomOut", "transition_in": "fadeSlow",  "transition_out": "fade"},
]


# ── Headers ───────────────────────────────────────────────────
def _headers() -> dict:
    return {
        "x-api-key":    settings.shotstack_api_key,
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }


# ── Photo clip builder ────────────────────────────────────────
def _make_photo_clip(url: str, start: float, duration: float, index: int) -> dict:
    style = SLIDESHOW_EFFECTS[min(index, len(SLIDESHOW_EFFECTS) - 1)]
    return {
        "asset": {"type": "image", "src": url},
        "start":  start,
        "length": duration,
        "effect": style["effect"],
        "transition": {
            "in":  style["transition_in"],
            "out": style["transition_out"],
        },
    }


# ── Slideshow (with optional outro) ──────────────────────────
def build_ad_timeline_photo_only(
    audio_url: str,
    car_photo_urls: list[str],
    dealership_name: str,
    vehicle_summary: str,
    feature_highlights: list[str],
    duration: float = 24.5,
    brand_color: str = DEFAULT_BRAND_COLOR,
    outro_video_url: Optional[str] = None,
    outro_duration: float = 10.0,
    slideshow_volume: float = 1.0,
    language: str = 'en',
) -> dict:
    """
    Build a Shotstack timeline for a slideshow ad with optional outro clip.

    Args:
        audio_url:        ElevenLabs voiceover URL (spans slideshow only)
        car_photo_urls:   Ordered list of car photo URLs
        dealership_name:  Fallback text for feature highlights
        vehicle_summary:  Displayed in top-left overlay during slideshow
        feature_highlights: Up to 3 feature strings (unused visually, reserved)
        duration:         Audio/slideshow length in seconds
        brand_color:      Hex brand colour for overlays
        outro_video_url:  Optional user-recorded outro clip URL
        outro_duration:   Length of outro clip in seconds (default 10s)

    The vehicle name overlay and "Message Me Today" CTA overlay appear only
    during the slideshow. The outro clip fades in and plays with no overlays —
    it is the CTA.
    """
    max_photos  = min(7, max(3, int(duration / 3.0)))
    photos      = list(car_photo_urls[:max_photos]) or [""]
    num_photos  = len(photos)
    photo_len   = round(duration / num_photos, 2)
    photo_starts = [round(i * photo_len, 2) for i in range(num_photos)]

    v_display = vehicle_summary[:40] + "…" if len(vehicle_summary) > 40 else vehicle_summary

    clips = []

    # ── Audio — slideshow duration only ──────────────────────
    clips.append({
        "asset": {"type": "audio", "src": audio_url, "volume": slideshow_volume},
        "start":  0,
        "length": duration,
    })

    # ── Car photo slideshow ───────────────────────────────────
    for i, (url, start) in enumerate(zip(photos, photo_starts)):
        clips.append(_make_photo_clip(url, start, photo_len, i))

    # ── Vehicle name overlay — top-left, slideshow only ───────
    clips.append({
        "asset": {
            "type":   "html",
            "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:26px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{v_display}</p>',
            "width":  600,
            "height": 60,
            "css":    "",
        },
        "position": "topLeft",
        "offset":   {"x": 0.0, "y": 0.0},
        "start":    0,
        "length":   duration,
    })

    # ── CTA overlay — bottom-left, slideshow only ─────────────
    # Omitted when an outro clip is present — the outro IS the CTA.
    if not outro_video_url:
        cta_text = '&#x1F4AC; Contáctame hoy' if language == 'es' else '&#x1F4AC; Message Me Today'
        clips.append({
            "asset": {
                "type":   "html",
                "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:24px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{cta_text}</p>',
                "width":  500,
                "height": 60,
                "css":    "",
            },
            "position": "bottomLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

    # ── Outro clip — appended after slideshow, fades in ───────
    if outro_video_url:
        clips.append({
            "asset": {
                "type":   "video",
                "src":    outro_video_url,
                "trim":   0,
            },
            "start":  duration,
            "length": outro_duration,
            "transition": {"in": "fade"},
        })

    return {
        "timeline": {
            "background": "#000000",
            "tracks":     [{"clips": clips}],
        },
        "output": {
            "format":     "mp4",
            "resolution": "hd",
            "fps":        30,
            "quality":    "high",
        },
    }


# ── Submit render ─────────────────────────────────────────────
async def submit_render(timeline: dict) -> str:
    render_request = dict(timeline)

    # Only register webhook in production — localhost isn't reachable by Shotstack
    if settings.environment == "production":
        render_request["callback"] = "https://api.dealersorbit.com/api/v1/jobs/webhook/shotstack"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            SHOTSTACK_URL,
            headers=_headers(),
            json=render_request,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Shotstack render submit failed: "
                f"{response.status_code} {response.text}"
            )
        data = response.json()
        render_id = data.get("response", {}).get("id")
        if not render_id:
            raise RuntimeError(
                f"Shotstack did not return a render_id. Response: {data}"
            )
        return render_id


# ── Delete render ─────────────────────────────────────────────
async def delete_render(render_id: str) -> bool:
    """Delete a completed render from Shotstack to free storage."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            SHOTSTACK_STATUS_URL.format(render_id=render_id),
            headers=_headers(),
        )
        return resp.status_code in (200, 204)


# ── Poll status ───────────────────────────────────────────────
async def get_render_status(render_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            SHOTSTACK_STATUS_URL.format(render_id=render_id),
            headers=_headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Shotstack status check failed: "
                f"{response.status_code} {response.text}"
            )
        data = response.json().get("response", {})
        return {
            "status": data.get("status", "unknown"),
            "url":    data.get("url"),
            "error":  data.get("error"),
        }


# ── Wait for completion ───────────────────────────────────────
async def wait_for_render(
    render_id: str,
    poll_interval: int = 10,
    max_wait: int = 600,
) -> str:
    elapsed = 0
    while elapsed < max_wait:
        status_data = await get_render_status(render_id)
        status = status_data["status"]
        if status == "done":
            url = status_data.get("url")
            if not url:
                raise RuntimeError("Shotstack done but no URL returned.")
            return url
        if status == "failed":
            raise RuntimeError(
                f"Shotstack render failed: {status_data.get('error')}"
            )
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    raise RuntimeError(
        f"Shotstack timed out after {max_wait}s. render_id: {render_id}"
    )


# ── Webhook-first wait (fallback poll) ───────────────────────
async def wait_for_render_with_fallback(
    render_id:     str,
    poll_interval: int = 15,    # longer interval — webhook should fire first in prod
    max_wait:      int = 1200,  # 20 minute ceiling
) -> Optional[str]:
    """
    Wait for a Shotstack render with webhook as the primary signal.

    In production, Shotstack POSTs to our webhook the moment rendering finishes,
    so this fallback poll rarely runs. In dev (localhost), the webhook can't reach
    us so this always polls through.

    Returns the video URL if fallback polling detected completion.
    Returns None if the webhook already completed the job.
    """
    from sqlmodel import select
    from app.core.database import AsyncSessionLocal
    from app.models.job import Job, JobStatus

    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # Check whether the webhook already handled this render
        async with AsyncSessionLocal() as session:
            result = await session.exec(
                select(Job).where(Job.shotstack_render_id == render_id)
            )
            job = result.first()
            if job and job.status == JobStatus.COMPLETED:
                print(f"Webhook already completed job for render {render_id}")
                return None
            if job and job.status == JobStatus.FAILED:
                return None

        # Webhook hasn't fired yet — poll Shotstack directly
        try:
            status_data = await get_render_status(render_id)
            status = status_data["status"]

            if status == "done":
                print(f"Fallback poll: render {render_id} done")
                return status_data.get("url")

            if status == "failed":
                raise RuntimeError(f"Shotstack render failed: {status_data.get('error')}")

        except RuntimeError:
            raise
        except Exception as e:
            print(f"Fallback poll error (retrying): {e}")

    raise RuntimeError(f"Render timed out after {max_wait}s. render_id: {render_id}")


# ── Download video ────────────────────────────────────────────
async def download_render(video_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(video_url)
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to download Shotstack video: {response.status_code}"
            )
        return response.content
