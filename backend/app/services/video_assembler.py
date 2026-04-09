from __future__ import annotations

"""
Shotstack Video Assembly Service
──────────────────────────────────
Ad structure (back to the correct format):

  |-- Hook (15%) --|------ Photo slideshow (70%) ------|-- CTA (15%) --|
  | Avatar talking |  Car photo 1 | Car photo 2 | Photo 3 | Avatar talking |
  |________________|______________|_____________|_________|_______________|
  |<-------------- ElevenLabs audio runs the full length -------------->|

Avatar is muted — ElevenLabs audio is the single source of truth.
Avatar visuals are shown during hook and CTA only.
Photos fill the middle with ken burns effects and text overlays.

Lip sync works because:
  - Hook section shows avatar from trim=0 (matches audio start)
  - CTA section shows avatar trimmed to where CTA begins in the audio
  - Audio plays uninterrupted the whole time
  - Since avatar is muted, no double audio
"""

import httpx
import asyncio

from app.core.config import get_settings

settings = get_settings()

SHOTSTACK_URL        = "https://api.shotstack.io/stage/render"
SHOTSTACK_STATUS_URL = "https://api.shotstack.io/stage/render/{render_id}"

# ── Transition themes ─────────────────────────────────────────
TRANSITION_THEMES = {
    "smooth": {
        "avatar_in":     "fade",
        "avatar_out":    "fade",
        "photo_in":      "fadeSlow",
        "photo_out":     "fadeSlow",
        "photo_effects": ["zoomIn", "zoomIn", "zoomOut"],
    },
    "dynamic": {
        "avatar_in":     "fade",
        "avatar_out":    "fade",
        "photo_in":      "slideLeft",
        "photo_out":     "fade",
        "photo_effects": ["zoomIn", "zoomOut", "slideLeft"],
    },
    "energetic": {
        "avatar_in":     "fade",
        "avatar_out":    "fade",
        "photo_in":      "wipeLeft",
        "photo_out":     "fade",
        "photo_effects": ["zoomIn", "zoomIn", "zoomOut"],
    },
}

DEFAULT_BRAND_COLOR = "#C4122F"


def _headers() -> dict:
    return {
        "x-api-key": settings.shotstack_api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


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


def _dealership_html(dealership_name: str, brand_color: str = DEFAULT_BRAND_COLOR) -> str:
    return f"""<p style="
        font-family: 'Open Sans', sans-serif;
        font-size: 28px;
        font-weight: 600;
        color: #ffffff;
        background: {brand_color};
        padding: 10px 28px;
        border-radius: 4px;
        letter-spacing: 1px;
        text-transform: uppercase;
    ">{dealership_name}</p>"""


def _vehicle_name_html(vehicle_summary: str) -> str:
    return f"""<p style="
        font-family: 'Open Sans', sans-serif;
        font-size: 22px;
        font-weight: 400;
        color: #ffffff;
        background: rgba(0,0,0,0.55);
        padding: 6px 18px;
        border-radius: 4px;
    ">{vehicle_summary}</p>"""

def _make_photo_clip(url: str, start: float, duration: float, index: int) -> dict:
    """
    Create a Shotstack photo clip with walkaround motion effect.
    Alternates zoom direction to simulate camera movement around the car.
    """
    effects = [
        "zoomIn",
        "zoomOut",
        "slideLeft",
        "slideRight",
    ]
    effect = effects[index % len(effects)]

    return {
        "asset": {"type": "image", "src": url},
        "start":  start,
        "length": duration,
        "effect": effect,
        "transition": {
            "in":  "fade",
            "out": "fade",
        },
    }


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
    Build a car ad timeline: hook avatar → photo slideshow → CTA avatar.

    Args:
        avatar_video_url:  Public S3 URL of the HeyGen avatar video
        audio_url:         Public S3 URL of the ElevenLabs audio
        car_photo_urls:    3 car photo URLs
        dealership_name:   e.g. "JBA Kia"
        vehicle_summary:   e.g. "2022 Kia Forte GT Line"
        feature_highlights: 3 feature strings
        duration:          Total audio duration in seconds
        hook_pct:          Fraction of duration for hook (default 15%)
        cta_pct:           Fraction of duration for CTA (default 15%)
        transition_style:  "smooth" | "dynamic" | "energetic"
        brand_color:       Hex color for branding
    """
    theme = TRANSITION_THEMES.get(transition_style, TRANSITION_THEMES["dynamic"])

    # Support up to 7 photos (5 exterior + 2 interior)
    photos = list(car_photo_urls[:7])
    while len(photos) < 1:
        photos.append(photos[-1] if photos else "")

    highlights = list(feature_highlights[:3])
    while len(highlights) < 3:
        highlights.append(dealership_name)

    # ── Timing ────────────────────────────────────────────────
    hook_len         = round(duration * hook_pct, 2)
    cta_len          = round(duration * cta_pct, 2)
    photo_section    = round(duration - hook_len - cta_len, 2)
    num_photos       = len(photos)
    photo_len        = round(photo_section / num_photos, 2)

    hook_start       = 0
    photo_start      = hook_len
    cta_start        = duration - cta_len

    photo_starts     = [
        round(photo_start + (i * photo_len), 2)
        for i in range(num_photos)
    ]

    clips = []

    # ── ElevenLabs audio: full duration, single source ────────
    # This is the ONLY audio in the video.
    # Avatar is muted so we never get double audio.
    clips.append({
        "asset": {
            "type": "audio",
            "src": audio_url,
            "volume": 1,
        },
        "start": 0,
        "length": duration,
    })

    # ── Avatar: hook section ──────────────────────────────────
    # trim=0 → starts from beginning of avatar video
    # Lip sync: audio seconds 0→hook_len, avatar seconds 0→hook_len ✅
    clips.append({
        "asset": {
            "type": "video",
            "src": avatar_video_url,
            "trim": 0,
            "volume": 0,   # muted — audio handled above
        },
        "start": hook_start,
        "length": hook_len,
        "transition": {
            "in": theme["avatar_in"],
            "out": theme["avatar_out"],
        },
    })

    # ── Avatar: CTA section ───────────────────────────────────
    # trim=cta_start → shows avatar mouth movements for the CTA words
    # Lip sync: audio seconds cta_start→end, avatar trimmed to same point ✅
    clips.append({
        "asset": {
            "type": "video",
            "src": avatar_video_url,
            "trim": cta_start,
            "volume": 0,   # muted — audio handled above
        },
        "start": cta_start,
        "length": cta_len,
        "transition": {
            "in": theme["avatar_in"],
            "out": "fade",
        },
    })

    # ── Car photo slideshow (walkaround motion) ───────────────
    for i, (photo_url, start) in enumerate(zip(photos, photo_starts)):
        clips.append(_make_photo_clip(photo_url, start, photo_len, i))

    # ── Feature text overlays (one per photo) ─────────────────
    for text, start in zip(highlights, photo_starts):
        clips.append({
            "asset": {
                "type": "html",
                "html": _feature_text_html(text, brand_color),
                "width": 800,
                "height": 120,
            },
            "position": "bottomLeft",
            "offset": {"x": 0.0, "y": 0.08},
            "start": start + 0.5,
            "length": photo_len - 1.0,
            "transition": {"in": "slideRight", "out": "fade"},
        })

    # ── Vehicle name across photo section ─────────────────────
    clips.append({
        "asset": {
            "type": "html",
            "html": _vehicle_name_html(vehicle_summary),
            "width": 700,
            "height": 60,
        },
        "position": "topLeft",
        "offset": {"x": 0.02, "y": -0.42},
        "start": photo_start,
        "length": photo_section,
        "transition": {"in": "fade", "out": "fade"},
    })

    # ── Dealership lower third (during CTA) ───────────────────
    clips.append({
        "asset": {
            "type": "html",
            "html": _dealership_html(dealership_name, brand_color),
            "width": 500,
            "height": 80,
        },
        "position": "bottomLeft",
        "offset": {"x": 0.0, "y": 0.05},
        "start": cta_start + 1.0,
        "length": cta_len - 1.5,
        "transition": {"in": "slideRight", "out": "fade"},
    })

    return {
        "timeline": {
            "background": "#000000",
            "tracks": [{"clips": clips}],
        },
        "output": {
            "format": "mp4",
            "resolution": "hd",
            "fps": 25,
            "quality": "medium",
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
            "url": data.get("url"),
            "error": data.get("error"),
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