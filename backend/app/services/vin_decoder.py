from __future__ import annotations

"""
VIN Decoder Service
────────────────────
Calls the free NHTSA vPIC API to decode a 17-character VIN.
No API key required.

The raw NHTSA response has ~130 fields, most empty.
We normalize it into a clean dict that the rest of the app uses.

To swap providers later (e.g. decode.vin for richer data),
add a new _decode_* function and point decode_vin() at it.
"""

import httpx
from typing import Optional

# NHTSA vPIC API — free, no key needed
NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"


async def decode_vin(vin: str) -> dict:
    """
    Decode a 17-character VIN and return a clean vehicle data dict.

    Example return value:
        {
            "vin": "1HGBH41JXMN109186",
            "year": "2021",
            "make": "Honda",
            "model": "Civic",
            "trim": "Sport",
            "body_style": "Sedan",
            "engine": "1.5L I4",
            "drivetrain": "FWD",
            "fuel_type": "Gasoline",
            "doors": "4"
        }

    Raises:
        ValueError — if VIN is wrong length or NHTSA can't decode it
        RuntimeError — if the HTTP request fails
    """
    # Basic validation before making the API call
    vin = vin.strip().upper()
    if len(vin) != 17:
        raise ValueError(f"VIN must be exactly 17 characters. You provided {len(vin)}.")

    return await _decode_nhtsa(vin)


async def _decode_nhtsa(vin: str) -> dict:
    """
    Internal function that calls the NHTSA API and normalizes the response.
    """
    url = NHTSA_URL.format(vin=vin)

    # httpx.AsyncClient is like the requests library but async
    # timeout=10 means give up if NHTSA doesn't respond in 10 seconds
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()  # raises if status is 4xx or 5xx
        except httpx.RequestError as e:
            raise RuntimeError(f"Could not reach NHTSA API: {e}") from e

    data = response.json()
    results = data.get("Results", [])

    if not results:
        raise ValueError("NHTSA returned no results for this VIN.")

    # NHTSA always returns a list with one item
    r = results[0]

    # Helper — NHTSA returns empty strings for unknown fields.
    # This converts empty strings to None so we can check cleanly.
    def val(key: str) -> Optional[str]:
        v = r.get(key, "").strip()
        return v if v else None

    # Check NHTSA error codes.
    # Code "0" means success. Anything else means the VIN had issues.
    error_code = val("ErrorCode") or "0"
    if not error_code.startswith("0"):
        error_text = val("ErrorText") or "Unknown error"
        raise ValueError(f"NHTSA could not decode this VIN: {error_text}")

    # Build a human-readable engine string from the parts NHTSA provides
    # e.g. "3.8L V6" or "1.5L I4"
    engine_str = _build_engine_string(
        displacement=val("DisplacementL"),
        cylinders=val("EngineCylinders"),
    )

    return {
        "vin": vin,
        "year": val("ModelYear"),
        "make": val("Make"),
        "model": val("Model"),
        "trim": val("Trim"),
        "body_style": val("BodyClass"),
        "engine": engine_str,
        "drivetrain": val("DriveType"),
        "fuel_type": val("FuelTypePrimary"),
        "doors": val("Doors"),
        "transmission": val("TransmissionStyle"),
        "plant_country": val("PlantCountry"),
    }


def _build_engine_string(
    displacement: Optional[str],
    cylinders: Optional[str],
) -> Optional[str]:
    """
    Build a readable engine string like "3.8L V6" or "1.5L I4".
    Returns None if we don't have enough data.
    """
    parts = []

    if displacement:
        # Round to 1 decimal place — NHTSA sometimes returns "1.49999"
        try:
            parts.append(f"{float(displacement):.1f}L")
        except ValueError:
            parts.append(f"{displacement}L")

    if cylinders:
        try:
            n = int(cylinders)
            # Convention: I4, I6 for inline, V6, V8 for V-engines
            config = "I" if n <= 4 else "V"
            parts.append(f"{config}{n}")
        except ValueError:
            parts.append(cylinders)

    return " ".join(parts) if parts else None


def vehicle_summary(vd: dict) -> str:
    """
    Returns a short one-line description of the vehicle.
    Used in Claude's prompt so it knows what car it's writing about.

    Example: "2024 Kia Telluride SX SUV — 3.8L V6, AWD, Gasoline"
    """
    # Build the name part: "2024 Kia Telluride SX SUV"
    name_parts = [
        vd.get("year", ""),
        vd.get("make", ""),
        vd.get("model", ""),
    ]
    name = " ".join(p for p in name_parts if p)

    if vd.get("trim"):
        name += f" {vd['trim']}"
    if vd.get("body_style"):
        name += f" {vd['body_style']}"

    # Build the specs part: "3.8L V6, AWD, Gasoline"
    spec_parts = []
    if vd.get("engine"):
        spec_parts.append(vd["engine"])
    if vd.get("drivetrain"):
        spec_parts.append(vd["drivetrain"])
    if vd.get("fuel_type"):
        spec_parts.append(vd["fuel_type"])

    if spec_parts:
        return f"{name} — {', '.join(spec_parts)}"
    return name