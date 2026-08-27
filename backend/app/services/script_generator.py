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
    phone_number: Optional[str] = None,
    include_cta: bool = True,
    price: Optional[str] = None,
    language: str = 'en',
    user_id: Optional[int] = None,
) -> dict:
    """
    Generate a 30-second ad script for a vehicle using Claude.

    Args:
        vehicle_data:     Clean vehicle dict from vin_decoder.decode_vin()
        theme:            Creative direction e.g. "family", "outdoorsy"
        salesperson_name: Injected into script e.g. "I'm Yoseph"
        dealership_name:  Injected into CTA e.g. "come see us at JBA Kia"
        include_cta:      When False, omit the verbal CTA — used for with_outro
                          videos where the recorded outro clip handles the CTA

    Returns:
        Structured script dict (see module docstring above)

    Raises:
        RuntimeError on API failure or if Claude returns unparseable output
    """
    theme_key = theme.lower().strip()
    guidance = THEME_GUIDANCE.get(theme_key, theme)
    v_summary = vehicle_summary(vehicle_data)

    system_prompt = """You are an expert automotive advertising copywriter specializing in
dealership video ads. You write natural, conversational scripts that sound like a real
person talking — not a corporate announcement. Your scripts are punchy, benefit-focused,
and always end with a clear call to action."""

    if salesperson_name:
        sp_line = (
            f"The salesperson is {salesperson_name}. "
            "Do NOT say their name or use any first-person introduction like \"I am\", \"My name is\", or \"Hi I'm\". "
            "The voice is a narrator presenting the vehicle on their behalf."
        )
    else:
        sp_line = ""

    if include_cta:
        if phone_number:
            cta_rules = (
                f"- End with: \"Send me a message or call/text me at {phone_number}!\"\n"
                f"- The phone number must appear verbatim: {phone_number}"
            )
            cta_instruction = f"One closing sentence ending with 'call/text {phone_number}'"
        else:
            cta_rules = (
                "- The CTA must always be: \"Send me a message\" or \"DM me\" or \"Message me today\"\n"
                "- End with something like: \"Message me today and let's make a deal!\""
            )
            cta_instruction = "One closing sentence ending with a message/DM call to action — no dealership name"
    else:
        cta_rules = (
            "- Do NOT include any verbal call to action at the end\n"
            "- Do NOT say \"send me a message\", \"DM me\", \"message me\", or any variation\n"
            "- End the script naturally after the benefits — the outro video handles the CTA"
        )
        cta_instruction = "One natural closing sentence that wraps up the benefits — NO call to action, the outro video handles that"

    price_line = f"PRICE: {price}" if price else ""

    if language == 'es':
        language_instruction = (
            "IMPORTANT: Write the ENTIRE script in Spanish. "
            "Natural, conversational Latin American Spanish. "
            "Write ALL numbers in Spanish words as they would be spoken aloud — "
            "the script goes directly to a voiceover and digits are read in English by the TTS engine. "
            "Examples: "
            "'dos mil veintitrés' not '2023', "
            "'treinta mil ochocientos dólares' not '$30,800', "
            "'cincuenta y dos mil millas' not '52,000 miles', "
            "'dos punto cero litros' not '2.0L'. "
            "The only English allowed is the brand name (Kia, Acura, Honda, etc.)."
        )
    else:
        language_instruction = ""

    user_prompt = f"""Write a 30-second video ad script for a car salesperson posting on social media.

VEHICLE: {v_summary}
{price_line}
THEME: {theme} — focus on: {guidance}
{sp_line}

RULES:
- Natural and conversational — sounds like a real person talking about the car
- Total length: 60-75 words (fits exactly 30 seconds at natural speaking pace)
- No filler phrases like "look no further" or "don't miss out"
- The hook must grab attention in the first 3 seconds
- NEVER say "I'm [name]", "My name is", "Hi I'm", or any first-person name introduction
- NEVER mention the dealership name
- NEVER say "come visit us", "stop by", "our dealership", or "our lot"
{cta_rules}

{language_instruction}

Return ONLY a JSON object with these exact keys. No markdown, no explanation, just the JSON:
{{
    "hook": "One punchy opening sentence that immediately grabs attention (10-15 words)",
    "body": "Two or three sentences covering the key benefits that match the theme",
    "cta": "{cta_instruction}",
    "full_script": "The hook, body, and cta combined into one natural flowing paragraph"
}}"""

    # ── Call Claude ───────────────────────────────────────────
    try:
        message = await _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            timeout=60.0,  # fail fast instead of hanging (SDK default is 10 min)
        )

        # Log paid-API usage (fire-and-forget, never raises). Real token counts
        # from msg.usage, getattr-guarded so logging can't break the pipeline.
        # Logged here (right after the billed call returns) so a downstream JSON
        # parse failure still records the cost we actually incurred.
        try:
            from app.services.analytics import record_api_usage
            _u = getattr(message, "usage", None)
            await record_api_usage(
                "video_script_generation",
                user_id=user_id,
                quantity=1,
                input_tokens=getattr(_u, "input_tokens", None),
                output_tokens=getattr(_u, "output_tokens", None),
                model="claude-sonnet-4-6",
            )
        except Exception as usage_err:  # noqa: BLE001
            print(f"[usage] video_script_generation log failed: {usage_err}")

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