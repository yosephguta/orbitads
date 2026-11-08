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
import html as _html
from typing import Optional

import httpx

from app.core.config import get_settings

settings = get_settings()

SHOTSTACK_URL        = "https://api.shotstack.io/v1/render"
SHOTSTACK_STATUS_URL = "https://api.shotstack.io/v1/render/{render_id}"

DEFAULT_BRAND_COLOR = "#C4122F"
DEFAULT_BAND_COLOR  = "#0d1f3c"  # brand navy — top/bottom band background

# ── Vertical (9:16) canvas geometry ──────────────────────────────
# Fixed band heights so every video looks consistent regardless of the
# source photo aspect ratio. Opaque bands sit ABOVE the photos (own track)
# so the Ken Burns zoom can't bleed into them.
CANVAS_W        = 1080
CANVAS_H        = 1920
TOP_BAND_H      = 340   # top band: y 0 → 340
BOTTOM_BAND_H   = 560   # bottom band: y 1360 → 1920
# Reels overlays the bottom ~20% (≈ y 1536+) and the right edge with its own
# buttons/caption. The CTA is lifted above that zone and kept horizontally
# centered (narrow box) so it never collides with the right-edge buttons.

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
        # "contain" scales the (landscape) photo to full canvas width, centered
        # vertically, with no cropping — the empty top/bottom show the band color.
        "asset": {"type": "image", "src": url},
        "fit":    "contain",
        "start":  start,
        "length": duration,
        "effect": style["effect"],
        "transition": {
            "in":  style["transition_in"],
            "out": style["transition_out"],
        },
    }


# ── Vertical band / text helpers ─────────────────────────────────
def _hex_luminance(hexstr: str) -> float:
    h = (hexstr or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return 0.0
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 0.0
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _text_color_for_bg(hexstr: str) -> str:
    """Pick near-black or white text for legibility on the chosen band color."""
    return "#111111" if _hex_luminance(hexstr) > 0.6 else "#ffffff"


def _fit_font_size(text: str, large: int, small: int, tiny: int) -> int:
    n = len(text or "")
    if n <= 26:
        return large
    if n <= 45:
        return small
    return tiny


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _band_rect_clip(band_color: str, height: int, position: str, duration: float) -> dict:
    """A solid, full-width opaque band anchored to the top or bottom."""
    return {
        "asset": {
            "type":   "html",
            "html":   f'<div style="width:{CANVAS_W}px;height:{height}px;background:{band_color}"></div>',
            "width":  CANVAS_W,
            "height": height,
            "css":    "",
        },
        "position": position,
        "offset":   {"x": 0.0, "y": 0.0},
        "start":    0,
        "length":   duration,
    }


def _band_text_clip(
    text: str,
    band_color: str,
    font_size: int,
    position: str,
    offset_y: float,
    start: float,
    length: float,
    box_w: int,
    box_h: int,
) -> dict:
    """Centered text drawn over a band."""
    color = _text_color_for_bg(band_color)
    safe  = _html.escape(text)
    inner = (
        f'<div style="display:flex;align-items:center;justify-content:center;'
        f'width:{box_w}px;height:{box_h}px;text-align:center">'
        f'<p style="font-family:Open Sans,sans-serif;font-size:{font_size}px;'
        f'font-weight:700;color:{color};margin:0;padding:0 24px;line-height:1.15;'
        f'text-shadow:0 2px 6px rgba(0,0,0,0.35)">{safe}</p>'
        f'</div>'
    )
    return {
        "asset": {"type": "html", "html": inner, "width": box_w, "height": box_h, "css": ""},
        "position": position,
        "offset":   {"x": 0.0, "y": offset_y},
        "start":    start,
        "length":   length,
    }


def _cta_text(cta_mode: str, cta_custom_text: Optional[str], phone_number: Optional[str], language: str) -> str:
    es = language == "es"
    if cta_mode == "custom" and (cta_custom_text or "").strip():
        return cta_custom_text.strip()
    if cta_mode == "phone" and phone_number and str(phone_number).strip():
        p = str(phone_number).strip()
        return f"Llama o envía mensaje al {p}" if es else f"Call or text {p}"
    # dm — also the fallback when 'phone' is chosen but no number is saved
    return "Envíame un mensaje" if es else "DM me for details"


# ── Slideshow (with optional outro) ──────────────────────────
def timeline_max_photos(duration: float) -> int:
    """How many photos the slideshow actually uses for a given audio duration.
    Kept as a shared helper so the pipeline can re-host ONLY the photos that will
    appear in the render (not the whole scraped set)."""
    return min(7, max(3, int(duration / 3.0)))


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
    # ── Video-style settings (per user, from /auth/me) ──────────
    hook_text: str = "",
    band_color: str = DEFAULT_BAND_COLOR,
    top_text_mode: str = "hook_then_vehicle",
    top_custom_text: Optional[str] = None,
    cta_mode: str = "phone",
    cta_custom_text: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> dict:
    """
    Build a Shotstack 9:16 (1080×1920) slideshow ad with optional outro clip.

    Layout (layered tracks, top layer first):
        1. Text   — top band text (hook→vehicle / vehicle / custom) + bottom CTA
        2. Bands  — opaque top & bottom bands in `band_color`
        3. Photos — full-width, centered, no crop (fit=contain) + outro at end
        4. Audio  — voiceover

    Bands + text span the slideshow only (start 0, length=duration). The outro
    plays alone after the slideshow with no bands — it is its own CTA. A
    landscape outro is scaled to full width centered on the band color (contain).
    """
    band_color = (band_color or DEFAULT_BAND_COLOR).strip() or DEFAULT_BAND_COLOR

    max_photos   = timeline_max_photos(duration)
    photos       = list(car_photo_urls[:max_photos]) or [""]
    num_photos   = len(photos)
    photo_len    = round(duration / num_photos, 2)
    photo_starts = [round(i * photo_len, 2) for i in range(num_photos)]

    vehicle_name = _shorten(vehicle_summary, 42)

    # ── Track 4: Audio — slideshow duration only ──────────────
    audio_clips = [{
        "asset":  {"type": "audio", "src": audio_url, "volume": slideshow_volume},
        "start":  0,
        "length": duration,
    }]

    # ── Track 3: Photos (+ outro appended) ────────────────────
    photo_clips = [
        _make_photo_clip(url, start, photo_len, i)
        for i, (url, start) in enumerate(zip(photos, photo_starts))
    ]
    if outro_video_url:
        photo_clips.append({
            # contain → vertical outro fills the frame; landscape outro is scaled
            # to full width centered on the band color (no crop). No bands overlap
            # the outro (band/text clips end at `duration`), so it plays full-frame.
            "asset": {"type": "video", "src": outro_video_url, "trim": 0},
            "fit":   "contain",
            "start":  duration,
            "length": outro_duration,
            "transition": {"in": "fade"},
        })

    # ── Track 2: Opaque bands ─────────────────────────────────
    band_clips = [
        _band_rect_clip(band_color, TOP_BAND_H, "top", duration),
        _band_rect_clip(band_color, BOTTOM_BAND_H, "bottom", duration),
    ]

    # ── Track 1: Text over the bands ──────────────────────────
    text_clips = []

    # Top band text
    if top_text_mode == "custom" and (top_custom_text or "").strip():
        txt = _shorten(top_custom_text, 60)
        text_clips.append(_band_text_clip(
            txt, band_color, _fit_font_size(txt, 46, 38, 30),
            "top", -0.005, 0, duration, 1000, 300,
        ))
    elif top_text_mode == "vehicle_only":
        text_clips.append(_band_text_clip(
            vehicle_name, band_color, _fit_font_size(vehicle_name, 48, 40, 34),
            "top", -0.005, 0, duration, 1000, 300,
        ))
    else:  # hook_then_vehicle (default)
        hook = _shorten(hook_text, 90)
        if hook:
            hook_len = 3.0 if duration > 4.0 else round(duration / 2, 2)
            text_clips.append(_band_text_clip(
                hook, band_color, _fit_font_size(hook, 44, 36, 30),
                "top", -0.005, 0, hook_len, 1000, 300,
            ))
            text_clips.append(_band_text_clip(
                vehicle_name, band_color, _fit_font_size(vehicle_name, 48, 40, 34),
                "top", -0.005, hook_len, round(duration - hook_len, 2), 1000, 300,
            ))
        else:
            text_clips.append(_band_text_clip(
                vehicle_name, band_color, _fit_font_size(vehicle_name, 48, 40, 34),
                "top", -0.005, 0, duration, 1000, 300,
            ))

    # Bottom band CTA — lifted above the Reels safe zone, narrow+centered to
    # clear the right-edge buttons.
    cta = _shorten(_cta_text(cta_mode, cta_custom_text, phone_number, language), 60)
    text_clips.append(_band_text_clip(
        cta, band_color, _fit_font_size(cta, 40, 34, 28),
        "bottom", 0.21, 0, duration, 860, 130,
    ))

    tracks = [
        {"clips": text_clips},
        {"clips": band_clips},
        {"clips": photo_clips},
        {"clips": audio_clips},
    ]

    return {
        "timeline": {
            "background": band_color,
            "tracks":     tracks,
        },
        "output": {
            "format":      "mp4",
            "size":        {"width": CANVAS_W, "height": CANVAS_H},
            "aspectRatio": "9:16",
            "fps":         30,
            "quality":     "high",
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
    poll_interval: Optional[int] = None,  # None → env-aware (see below)
    max_wait:      int = 1200,  # 20 minute ceiling
) -> Optional[str]:
    """
    Wait for a Shotstack render with webhook as the primary signal.

    In production, Shotstack POSTs to our webhook the moment rendering finishes,
    so this fallback poll rarely runs — keep it slow (15s) to avoid redundant
    Shotstack polls at scale. In dev (localhost) the webhook can't reach us, so
    this poll IS the only completion signal — use a short 5s interval so the
    extension sees the finished video quickly instead of ~15s+ late.

    Returns the video URL if fallback polling detected completion.
    Returns None if the webhook already completed the job.
    """
    from sqlmodel import select
    from app.core.database import AsyncSessionLocal
    from app.models.job import Job, JobStatus

    if poll_interval is None:
        poll_interval = 5 if settings.environment == "development" else 15

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
