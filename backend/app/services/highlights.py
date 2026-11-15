from __future__ import annotations

"""
Dealership-tab vehicle highlights — Claude-generated salesperson talking points.

Audience: a car salesperson about to talk to a customer. 3–4 short talking
points (title + 1–2 benefit sentences), English or Spanish. Generated on demand
the first time a car is opened, cached per (inventory_id, language), regenerated
only when the car's key data changes (tracked via `compute_input_hash`) or when
PROMPT_VERSION bumps.

GUARDRAILS (enforced in the prompt + defended in parsing):
  - Use ONLY the provided inventory fields, NHTSA data, the detail-page
    specs/highlights/packages, plus well-known facts true for EVERY trim of that
    year/make/model.
  - Never claim an option/package/feature not in the provided data; never quote
    figures (MPG, hp, towing) not provided; never compare to competitors; never
    mention price beyond the given final price; never promise warranty terms.
  - Anything trim/options-dependent and not confirmed by the data → verify=True
    (the UI shows "verify on the sticker").
  - When there are no detail-page specs (data_quality='basic'): lean on
    model-level strengths + mileage/condition, keep `verify` honest.

The route (inventory.py) owns caching/DB; this module owns the prompt, the hash,
and the Claude call (which logs api_usage as `inventory_highlights`).
"""

import hashlib
import json
import re
from typing import Optional

import anthropic

from app.core.config import get_settings

settings = get_settings()

# Bump to invalidate every cached highlight (e.g. after a prompt change).
PROMPT_VERSION = "1"
MODEL = "claude-sonnet-4-6"

# Shape/content limits defended in parsing.
_MAX_POINTS = 4
_MIN_POINTS = 3
_TITLE_CAP = 80
_DETAIL_CAP = 400

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


class HighlightsUnavailable(Exception):
    """Raised when Claude fails or returns unusable output. Route → 502."""


# ── Specs / data-quality helpers ──────────────────────────────
def _specs_from_row(row) -> dict:
    if not row.specs_json:
        return {}
    try:
        data = json.loads(row.specs_json)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def data_quality(row) -> str:
    """'detail_page' when the row carries captured VDP specs, else 'basic'."""
    specs = _specs_from_row(row)
    has_specs = bool(
        specs.get("spec_table") or specs.get("highlights") or specs.get("packages")
    )
    return "detail_page" if has_specs else "basic"


def _nhtsa_from_row(row) -> dict:
    if not row.vehicle_data:
        return {}
    try:
        data = json.loads(row.vehicle_data)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


# ── Input hash (cache key) ────────────────────────────────────
def compute_input_hash(row, language: str) -> str:
    """
    sha256 over everything that can change the generated highlights: the core
    inventory fields, the NHTSA-derived fields used, specs_json, the language,
    and PROMPT_VERSION. A new price/mileage/spec capture changes the hash → the
    next GET regenerates automatically.
    """
    vd = _nhtsa_from_row(row)
    payload = {
        "year": row.year,
        "make": row.make,
        "model": row.model,
        "trim": row.trim,
        "condition": row.condition,
        "price": row.price,
        "mileage": row.mileage,
        "body_class": row.body_class,
        # NHTSA fields used in the prompt
        "drivetrain": row.drivetrain,
        "fuel_type": row.fuel_type,
        "engine": row.engine,
        "transmission": vd.get("transmission"),
        "doors": vd.get("doors"),
        "specs_json": row.specs_json or "",
        "language": language,
        "prompt_version": PROMPT_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── Prompt ────────────────────────────────────────────────────
def _vehicle_line(row) -> str:
    parts = [str(row.year) if row.year else "", row.make or "", row.model or "", row.trim or ""]
    return " ".join(p for p in parts if p).strip() or "this vehicle"


def _facts_block(row) -> str:
    vd = _nhtsa_from_row(row)
    lines = []

    def add(label, val):
        if val is not None and str(val).strip() != "":
            lines.append(f"- {label}: {val}")

    add("Condition", row.condition)
    add("Mileage", f"{row.mileage:,} miles" if row.mileage is not None else None)
    add("Final advertised price", f"${row.price:,}" if row.price is not None else None)
    add("Body style", row.body_class or row.body_style)
    add("Drivetrain", row.drivetrain)
    add("Engine", row.engine)
    add("Fuel type", row.fuel_type)
    add("Transmission", vd.get("transmission"))
    add("Exterior color", row.exterior_color)
    add("Interior color", row.interior_color)
    return "\n".join(lines) if lines else "- (no additional decoded facts)"


def _specs_block(row) -> str:
    specs = _specs_from_row(row)
    chunks = []

    spec_table = specs.get("spec_table")
    if isinstance(spec_table, dict) and spec_table:
        rows = [f"  {k}: {v}" for k, v in spec_table.items() if str(v).strip()]
        if rows:
            chunks.append("Spec table:\n" + "\n".join(rows))

    highlights = specs.get("highlights")
    if isinstance(highlights, list) and highlights:
        items = [f"  - {h}" for h in highlights if str(h).strip()]
        if items:
            chunks.append("Factory/vehicle highlights (confirmed equipment):\n" + "\n".join(items))

    packages = specs.get("packages")
    if isinstance(packages, list) and packages:
        pk_lines = []
        for p in packages:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).strip()
            items = [str(i).strip() for i in (p.get("items") or []) if str(i).strip()]
            if name or items:
                pk_lines.append(f"  {name or 'Package'}: {', '.join(items)}")
        if pk_lines:
            chunks.append("Packages (confirmed):\n" + "\n".join(pk_lines))

    return "\n\n".join(chunks) if chunks else ""


def _build_prompt(row, language: str, dq: str):
    vehicle = _vehicle_line(row)
    facts = _facts_block(row)
    specs = _specs_block(row)

    system_prompt = (
        "You are an expert automotive sales coach. You write short, accurate "
        "talking points that help a salesperson confidently describe a specific "
        "vehicle to a customer on the lot. You are meticulous about never "
        "inventing features — a salesperson repeating a claim that isn't true "
        "loses the customer's trust and can create a legal problem for the "
        "dealership."
    )

    if language == "es":
        lang_rule = (
            "Write EVERYTHING (titles and details) in natural, conversational "
            "Latin American Spanish as a dealership salesperson would actually "
            "speak it — not a literal translation."
        )
    else:
        lang_rule = "Write everything in natural, conversational English."

    if dq == "basic":
        data_rule = (
            "There are NO captured detail-page specs for this car. Base the "
            "talking points on model-level strengths that are true for every "
            "trim of this year/make/model, plus the condition/mileage/price "
            "facts above. Be conservative — mark anything trim- or option-"
            "dependent with verify=true."
        )
    else:
        data_rule = (
            "Use the confirmed detail-page specs/highlights/packages above as the "
            "primary source. Equipment listed there is confirmed for THIS car."
        )

    specs_section = ("CONFIRMED DETAIL-PAGE DATA:\n" + specs) if specs else "CONFIRMED DETAIL-PAGE DATA: (none captured)"

    user_prompt = f"""Write {_MIN_POINTS}-{_MAX_POINTS} customer-facing talking points for this vehicle.

VEHICLE: {vehicle}

KNOWN FACTS (from inventory + NHTSA decode):
{facts}

{specs_section}

{data_rule}
{lang_rule}

STRICT RULES — follow every one:
- Each talking point = a short TITLE plus 1-2 sentences of DETAIL, framed as a benefit to the customer.
- Use ONLY the facts/specs above, plus well-known facts true for EVERY trim of this exact year/make/model.
- NEVER claim an option, package, or feature that is not in the data above.
- NEVER quote numbers (MPG, horsepower, towing capacity, range) unless that exact number appears above.
- NEVER compare to other brands or vehicles.
- NEVER mention price except the single final advertised price shown above (and only if useful).
- NEVER promise warranty terms or coverage.
- If a point depends on trim or optional equipment and is NOT confirmed in the data above, set "verify": true (the app shows "verify on the sticker"). If the point is confirmed by the data above or is universally true for this model, set "verify": false.

Return ONLY a JSON object, no markdown, no commentary:
{{
  "highlights": [
    {{"title": "short title", "detail": "1-2 benefit sentences", "verify": false}}
  ]
}}"""

    return system_prompt, user_prompt


# ── Claude call + parse ───────────────────────────────────────
def _parse(raw: str) -> Optional[list]:
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    items = obj.get("highlights") if isinstance(obj, dict) else None
    if not isinstance(items, list) or not items:
        return None
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()[:_TITLE_CAP]
        detail = str(it.get("detail", "")).strip()[:_DETAIL_CAP]
        if not title and not detail:
            continue
        cleaned.append({
            "title": title,
            "detail": detail,
            "verify": bool(it.get("verify", False)),
        })
    if not cleaned:
        return None
    return cleaned[:_MAX_POINTS]


async def _call_claude(system_prompt: str, user_prompt: str, user_id: Optional[int]) -> str:
    message = await _client.messages.create(
        model=MODEL,
        max_tokens=900,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        timeout=60.0,
    )
    # Log paid usage right after the billed call returns (before parse), guarded.
    try:
        from app.services.analytics import record_api_usage
        _u = getattr(message, "usage", None)
        await record_api_usage(
            "inventory_highlights",
            user_id=user_id,
            quantity=1,
            input_tokens=getattr(_u, "input_tokens", None),
            output_tokens=getattr(_u, "output_tokens", None),
            model=MODEL,
        )
    except Exception as usage_err:  # noqa: BLE001
        print(f"[usage] inventory_highlights log failed: {usage_err}")
    return message.content[0].text


async def generate_highlights(row, language: str, user_id: Optional[int]) -> dict:
    """
    Call Claude and return {"highlights": [{title, detail, verify}], "model": MODEL}.
    Parses defensively with ONE retry on a parse failure. Raises
    HighlightsUnavailable on any API error or if both attempts are unparseable —
    the route maps that to 502 and does NOT cache the failure.
    """
    dq = data_quality(row)
    system_prompt, user_prompt = _build_prompt(row, language, dq)

    try:
        raw = await _call_claude(system_prompt, user_prompt, user_id)
        parsed = _parse(raw)
        if parsed is None:
            # one retry (a nudge to return pure JSON)
            raw = await _call_claude(
                system_prompt,
                user_prompt + "\n\nIMPORTANT: return ONLY the JSON object, nothing else.",
                user_id,
            )
            parsed = _parse(raw)
    except anthropic.APIError as e:
        raise HighlightsUnavailable(str(e)) from e

    if parsed is None:
        raise HighlightsUnavailable("Claude returned unparseable highlights")

    return {"highlights": parsed, "model": MODEL}
