from __future__ import annotations

"""
Script Generator Service
─────────────────────────
Calls the Claude API to generate a 30-second ad script for a specific
vehicle and theme. The script is structured as JSON so each section
(hook, body, CTA) can be used independently by the video assembly pipeline.

Output shape:
    {
        "hook":        "Opening line — grabs attention in first 3 seconds",
        "body":        "2-3 sentences covering the key benefits",
        "cta":         "Closing line with dealership name and next step",
        "full_script": "All three sections as one flowing paragraph",
        "word_count":  74,
        "theme":       "family",
        "vehicle":     "2022 Kia Forte GT Line Sedan — 2.0L I4, Gasoline"
    }
"""

import json
import re
from typing import Optional

import anthropic

from app.core.config import get_settings
from app.services.vin_decoder import vehicle_summary

settings = get_settings()

# Anthropic client — created once, reused for every request
_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# ── Theme guidance ────────────────────────────────────────────
# These words are injected into Claude's prompt to steer the tone.
# Each theme produces a noticeably different script for the same car.
THEME_GUIDANCE: dict[str, str] = {
    "family":    "safety ratings, passenger space, comfort, reliability, school runs, road trips, car seats",
    "outdoorsy": "adventure, trail capability, rugged reliability, weekend getaways, cargo space, freedom",
    "luxury":    "premium materials, smooth quiet ride, advanced tech features, refined comfort, prestige",
    "sporty":    "acceleration, handling, driver experience, performance specs, excitement, responsive",
    "commuter":  "fuel efficiency, low cost of ownership, reliability, easy parking, tech connectivity",
    "eco":       "fuel economy, low emissions, cost savings at the pump, environmental responsibility",
    "first_car": "affordable payments, easy to drive, great value, low insurance, reliable, fun",
}


async def generate_ad_script(
    vehicle_data: dict,
    theme: str,
    salesperson_name: Optional[str] = None,
    dealership_name: Optional[str] = None,
) -> dict:
    """
    Generate a 30-second ad script for a vehicle using Claude.

    Args:
        vehicle_data:     Clean vehicle dict from vin_decoder.decode_vin()
        theme:            Creative direction e.g. "family", "outdoorsy"
        salesperson_name: Injected into script e.g. "I'm Yoseph"
        dealership_name:  Injected into CTA e.g. "come see us at JBA Kia"

    Returns:
        Structured script dict (see module docstring above)

    Raises:
        RuntimeError on API failure or if Claude returns unparseable output
    """
    # Get the theme guidance words, fall back to the raw theme if not in our list
    theme_key = theme.lower().strip()
    guidance = THEME_GUIDANCE.get(theme_key, theme)

    # Build the vehicle summary line Claude will see
    v_summary = vehicle_summary(vehicle_data)

    # Build optional context lines
    sp_line = f"The salesperson's name is {salesperson_name}." if salesperson_name else ""
    dealer_line = f"The dealership is {dealership_name}." if dealership_name else ""

    # ── System prompt ─────────────────────────────────────────
    # Sets Claude's role and the rules it must follow
    system_prompt = """You are an expert automotive advertising copywriter specializing in 
dealership video ads. You write natural, conversational scripts that sound like a real 
person talking — not a corporate announcement. Your scripts are punchy, benefit-focused, 
and always end with a clear call to action."""

    # ── User prompt ───────────────────────────────────────────
    # The actual request — includes all the context Claude needs
    user_prompt = f"""Write a 30-second video ad script for a car dealership salesperson.

VEHICLE: {v_summary}
THEME: {theme} — focus on: {guidance}
{sp_line}
{dealer_line}

RULES:
- The salesperson speaks directly to camera in first person
- Natural and conversational — sounds like a real person, not an ad agency
- Total length: 70-80 words (this fits exactly 30 seconds at a natural speaking pace)
- No filler phrases like "look no further" or "don't miss out"
- The hook must grab attention in the first 3 seconds

Return ONLY a JSON object with these exact keys. No markdown, no explanation, just the JSON:
{{
    "hook": "One punchy opening sentence that immediately grabs attention (10-15 words)",
    "body": "Two or three sentences covering the key benefits that match the theme",
    "cta": "One closing sentence with a clear next step and the dealership name",
    "full_script": "The hook, body, and cta combined into one natural flowing paragraph"
}}"""

    # ── Call Claude ───────────────────────────────────────────
    try:
        message = await _client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
        )

        raw = message.content[0].text.strip()

        # Claude sometimes wraps JSON in markdown code fences even when asked not to.
        # Strip them if present.
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)

        # Validate that all required keys are present
        required_keys = {"hook", "body", "cta", "full_script"}
        missing = required_keys - set(parsed.keys())
        if missing:
            raise RuntimeError(f"Claude response is missing keys: {missing}")

        # Add metadata
        parsed["word_count"] = len(parsed["full_script"].split())
        parsed["theme"] = theme
        parsed["vehicle"] = v_summary

        return parsed

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude returned invalid JSON. Raw response: {raw[:300]}. Error: {e}"
        ) from e
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API error: {e}") from e