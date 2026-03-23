from __future__ import annotations

"""
ElevenLabs Voice Service
─────────────────────────
Handles two things:
  1. Voice cloning — creates a reusable voice from a recording (once per salesperson)
  2. Text to speech — converts ad script to audio using the cloned voice (per ad)

API docs: https://elevenlabs.io/docs/api-reference

Voice cloning flow:
  upload recording → ElevenLabs returns voice_id → save to user profile

TTS flow:
  script text + voice_id → ElevenLabs returns MP3 bytes → save to S3
"""

import httpx
from app.core.config import get_settings

settings = get_settings()

# ElevenLabs API base URL
BASE_URL = "https://api.elevenlabs.io/v1"


def _headers() -> dict:
    """Auth headers for every ElevenLabs request."""
    return {
        "xi-api-key": settings.elevenlabs_api_key,
        "Accept": "application/json",
    }


# ── Voice cloning ─────────────────────────────────────────────
async def clone_voice(
    audio_bytes: bytes,
    filename: str,
    voice_name: str,
    description: str = "",
) -> str:
    """
    Create a cloned voice from an audio recording.

    Called once when a salesperson first sets up their profile.
    The returned voice_id is saved to the database and reused for
    every ad that salesperson generates — no need to re-clone.

    Args:
        audio_bytes:  Raw bytes of the voice recording from S3
        filename:     Original filename e.g. "recording.mp3"
        voice_name:   Display name for this voice e.g. "Yoseph - JBA Kia"
        description:  Optional note about the voice

    Returns:
        voice_id string — save this to the user's profile

    Raises:
        RuntimeError on API failure
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{BASE_URL}/voices/add",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={
                "name": voice_name,
                "description": description,
            },
            files={
                # ElevenLabs expects the file as multipart form data
                "files": (filename, audio_bytes, "audio/mpeg"),
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs voice clone failed: {response.status_code} {response.text}"
            )

        data = response.json()
        voice_id = data.get("voice_id")

        if not voice_id:
            raise RuntimeError(
                f"ElevenLabs did not return a voice_id. Response: {data}"
            )

        return voice_id


# ── Text to speech ────────────────────────────────────────────
async def text_to_speech(
    text: str,
    voice_id: str,
    model_id: str = "eleven_turbo_v2",
) -> bytes:
    """
    Convert text to speech using a cloned voice.

    Called for every ad generation after the voice has been cloned.
    Returns raw MP3 bytes — save these to S3.

    Args:
        text:      The full ad script to convert to audio
        voice_id:  The cloned voice ID from clone_voice()
        model_id:  ElevenLabs model to use:
                     eleven_turbo_v2      — fast, good quality, lower cost ✅
                     eleven_multilingual_v2 — best quality, slower, higher cost

    Returns:
        MP3 audio bytes

    Raises:
        RuntimeError on API failure
    """
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            # stability: 0.0 = more expressive, 1.0 = more consistent
            # 0.5 is a good balance for ad scripts
            "stability": 0.5,
            # similarity_boost: how closely to match the original voice
            # 0.8 gives a good clone without sounding robotic
            "similarity_boost": 0.8,
            # style: adds expressiveness — keep low for professional ads
            "style": 0.2,
            # use_speaker_boost: improves voice clarity
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{BASE_URL}/text-to-speech/{voice_id}",
            headers={
                **_headers(),
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs TTS failed: {response.status_code} {response.text}"
            )

        # Response body is the raw MP3 bytes
        return response.content


# ── List voices ───────────────────────────────────────────────
async def list_voices() -> list[dict]:
    """
    Return all voices in the account.
    Useful for checking if a voice was cloned successfully
    or for debugging in development.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{BASE_URL}/voices",
            headers=_headers(),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs list voices failed: {response.status_code} {response.text}"
            )

        return response.json().get("voices", [])


# ── Delete voice ──────────────────────────────────────────────
async def delete_voice(voice_id: str) -> None:
    """
    Delete a cloned voice from ElevenLabs.
    Call this if a salesperson leaves or resets their voice.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            f"{BASE_URL}/voices/{voice_id}",
            headers=_headers(),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs delete voice failed: {response.status_code} {response.text}"
            )