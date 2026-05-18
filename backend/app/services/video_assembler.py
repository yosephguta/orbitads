from __future__ import annotations

"""
Shotstack Video Assembly Service
──────────────────────────────────
Two ad formats:

1. Avatar + Slideshow (premium):
  |-- Hook (15%) --|------ Photo slideshow (70%) ------|-- CTA (15%) --|
  | Avatar talking |  Car photos with motion effects   | Avatar talking |
  |<-------------- ElevenLabs audio runs the full length -------------->|

2. Slideshow only (free):
  |---------------------- Photo slideshow (100%) ----------------------|
  |              Car photos with motion effects + text overlays        |
  |<-------------- ElevenLabs audio runs the full length -------------->|
"""

import httpx
import asyncio

from app.core.config import get_settings

settings = get_settings()

SHOTSTACK_URL        = "https://api.shotstack.io/v1/render"
SHOTSTACK_STATUS_URL = "https://api.shotstack.io/v1/render/{render_id}"

DEFAULT_BRAND_COLOR  = "#C4122F"

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

TRANSITION_THEMES = {
    "smooth": {
        "avatar_in":  "fade",
        "avatar_out": "fade",
    },
    "dynamic": {
        "avatar_in":  "fade",
        "avatar_out": "fade",
    },
    "energetic": {
        "avatar_in":  "fade",
        "avatar_out": "fade",
    },
}


# ── Headers ───────────────────────────────────────────────────
def _headers() -> dict:
    return {
        "x-api-key":    settings.shotstack_api_key,
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }


# ── HTML helpers ──────────────────────────────────────────────
def _feature_text_html(text: str, brand_color: str = DEFAULT_BRAND_COLOR) -> str:
    return f"""<p style="
        font-family: 'Open Sans', sans-serif;
        font-size: 36px;
        font-weight: 700;
        color: #ffffff;
        background: linear-gradient(90deg, {brand_color}ee 0%, rgba(0,0,0,0.75) 100%);
        padding: 12px 24px 12px 20px;
        border-left: 6px solid {brand_color};
        border-radius: 0 8px 8px 0;
        display: inline-block;
        text-shadow: 1px 1px 3px rgba(0,0,0,0.5);
    ">{text}</p>"""


def _dealership_html(cta_text: str = "💬 Message Me Today", brand_color: str = DEFAULT_BRAND_COLOR) -> str:
    return f"""<p style="
        font-family: 'Open Sans', sans-serif;
        font-size: 26px;
        font-weight: 700;
        color: #ffffff;
        background: rgba(0,0,0,0.85);
        padding: 10px 24px;
        margin: 0;
        display: block;
        white-space: nowrap;
    ">{cta_text}</p>"""


def _vehicle_name_html(vehicle_summary: str) -> str:
    display = vehicle_summary[:40] + "…" if len(vehicle_summary) > 40 else vehicle_summary
    return f"""<p style="
        font-family: 'Open Sans', sans-serif;
        font-size: 28px;
        font-weight: 800;
        color: #ffffff;
        background: rgba(0,0,0,0.85);
        padding: 10px 20px;
        margin: 0;
        display: block;
        white-space: nowrap;
    ">{display}</p>"""


# ── Photo clip builder ────────────────────────────────────────
def _make_photo_clip(url: str, start: float, duration: float, index: int) -> dict:
    """Create a polished slideshow photo clip with subtle motion."""
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


# ── Format 1: Avatar + Slideshow (premium) ───────────────────
def build_ad_timeline(
    avatar_video_url: str,
    audio_url: str,
    car_photo_urls: list[str],
    dealership_name: str,
    vehicle_summary: str,
    feature_highlights: list[str],
    duration: float = 24.5,
    hook_pct: float = 0.15,
    cta_pct: float = 0.15,
    transition_style: str = "dynamic",
    brand_color: str = DEFAULT_BRAND_COLOR,
) -> dict:
    """
    Premium ad: hook avatar → photo slideshow → CTA avatar.
    Avatar is muted — ElevenLabs audio is the single audio source.
    """
    theme = TRANSITION_THEMES.get(transition_style, TRANSITION_THEMES["dynamic"])

    # Timing
    hook_len      = round(duration * hook_pct, 2)
    cta_len       = round(duration * cta_pct, 2)
    photo_section = round(duration - hook_len - cta_len, 2)

    hook_start    = 0
    photo_start   = hook_len
    cta_start     = duration - cta_len

    # Photos — cap at 7, minimum 3s each
    max_photos  = min(7, max(3, int(photo_section / 3.0)))
    photos      = list(car_photo_urls[:max_photos])
    while len(photos) < 1:
        photos.append(photos[-1] if photos else "")

    num_photos  = len(photos)
    photo_len   = round(photo_section / num_photos, 2)
    photo_starts = [round(photo_start + i * photo_len, 2) for i in range(num_photos)]

    highlights = list(feature_highlights[:3])
    while len(highlights) < 3:
        highlights.append(dealership_name)

    clips = []

    # Audio — full duration, single source
    clips.append({
        "asset": {"type": "audio", "src": audio_url, "volume": 1},
        "start":  0,
        "length": duration,
    })

    # Avatar hook — trim=0, lip syncs with audio start
    clips.append({
        "asset": {
            "type":   "video",
            "src":    avatar_video_url,
            "trim":   0,
            "volume": 0,
        },
        "start":  hook_start,
        "length": hook_len,
        "transition": {
            "in":  theme["avatar_in"],
            "out": theme["avatar_out"],
        },
    })

    # Avatar CTA — trimmed to cta_start, lip syncs with audio CTA
    clips.append({
        "asset": {
            "type":   "video",
            "src":    avatar_video_url,
            "trim":   cta_start,
            "volume": 0,
        },
        "start":  cta_start,
        "length": cta_len,
        "transition": {
            "in":  theme["avatar_in"],
            "out": "fade",
        },
    })

    # Car photo slideshow
    for i, (url, start) in enumerate(zip(photos, photo_starts)):
        clips.append(_make_photo_clip(url, start, photo_len, i))

      # ── Vehicle name — static top left ────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:26px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{vehicle_summary[:40]}</p>',
                "width":  600,
                "height": 60,
                "css":    "",
            },
            "position": "topLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

        # ── CTA — static bottom left ───────────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   '<p style="font-family:Open Sans,sans-serif;font-size:24px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">&#x1F4AC; Message Me Today</p>',
                "width":  500,
                "height": 60,
                "css":    "",
            },
            "position": "bottomLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

       # ── Vehicle name — static top left ────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:26px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{vehicle_summary[:40]}</p>',
                "width":  600,
                "height": 60,
                "css":    "",
            },
            "position": "topLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

        # ── CTA — static bottom left ───────────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   '<p style="font-family:Open Sans,sans-serif;font-size:24px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">&#x1F4AC; Message Me Today</p>',
                "width":  500,
                "height": 60,
                "css":    "",
            },
            "position": "bottomLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
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


# ── Format 2: Slideshow only (free) ──────────────────────────
def build_ad_timeline_photo_only(
    audio_url: str,
    car_photo_urls: list[str],
    dealership_name: str,
    vehicle_summary: str,
    feature_highlights: list[str],
    duration: float = 24.5,
    brand_color: str = DEFAULT_BRAND_COLOR,
) -> dict:
    """
    Free ad: polished photo slideshow with audio, no avatar.
    5 exterior + 2 interior photos with smooth motion effects.
    """
    # Cap at 7 photos, minimum 3s each
    max_photos  = min(7, max(3, int(duration / 3.0)))
    photos      = list(car_photo_urls[:max_photos])
    while len(photos) < 1:
        photos.append(photos[-1] if photos else "")

    highlights = list(feature_highlights[:3])
    while len(highlights) < 3:
        highlights.append(dealership_name)

    num_photos   = len(photos)
    photo_len    = round(duration / num_photos, 2)
    photo_starts = [round(i * photo_len, 2) for i in range(num_photos)]

    clips = []

    # Audio — full duration
    clips.append({
        "asset": {"type": "audio", "src": audio_url, "volume": 1},
        "start":  0,
        "length": duration,
    })

    # Car photo slideshow
    for i, (url, start) in enumerate(zip(photos, photo_starts)):
        clips.append(_make_photo_clip(url, start, photo_len, i))

    # ── Vehicle name — static top left ────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:26px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{vehicle_summary[:40]}</p>',
                "width":  600,
                "height": 60,
                "css":    "",
            },
            "position": "topLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

        # ── CTA — static bottom left ───────────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   '<p style="font-family:Open Sans,sans-serif;font-size:24px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">&#x1F4AC; Message Me Today</p>',
                "width":  500,
                "height": 60,
                "css":    "",
            },
            "position": "bottomLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

            # ── Vehicle name — static top left ────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   f'<p style="font-family:Open Sans,sans-serif;font-size:26px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">{vehicle_summary[:40]}</p>',
                "width":  600,
                "height": 60,
                "css":    "",
            },
            "position": "topLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
        })

        # ── CTA — static bottom left ───────────────────────────────
        clips.append({
            "asset": {
                "type":   "html",
                "html":   '<p style="font-family:Open Sans,sans-serif;font-size:24px;font-weight:700;color:#fff;background:rgba(0,0,0,0.8);padding:8px 16px;margin:0;white-space:nowrap">&#x1F4AC; Message Me Today</p>',
                "width":  500,
                "height": 60,
                "css":    "",
            },
            "position": "bottomLeft",
            "offset":   {"x": 0.0, "y": 0.0},
            "start":    0,
            "length":   duration,
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            SHOTSTACK_URL,
            headers=_headers(),
            json=timeline,
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


# ── Download video ────────────────────────────────────────────
async def download_render(video_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(video_url)
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to download Shotstack video: {response.status_code}"
            )
        return response.content