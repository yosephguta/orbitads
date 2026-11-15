"""
Body-style normalization — ported verbatim from the extension's
`normalizeBodyStyle` (orbitads-extension/background.js) so both sides agree on
the canonical `body_class` value.

Maps NHTSA's verbose strings ("Sport Utility Vehicle [SUV]/Multipurpose Vehicle
[MPV]", "Sedan/Saloon", "Pickup", ...) to a short canonical form ("SUV",
"Sedan", "Truck", ...). Used to set `dealership_inventory.body_class` whenever
`body_style` is written, and for the alternatives match + highlights hash.

Keep the mapping and the check ORDER identical to the JS (SUV/van/convertible
checks run before the generic ones). The one difference from the JS: the
passthrough for an unmapped value is truncated to 20 chars so it can never
overflow the `body_class` varchar(20) column (bug-safety for a future odd NHTSA
string — all current dev values map cleanly, none fall through).
"""
from typing import Optional

_MAX_LEN = 20  # body_class is varchar(20)


def normalize_body_style(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    lower = str(raw).lower()
    if "suv" in lower or "sport utility" in lower or "crossover" in lower:
        return "SUV"
    if "pickup" in lower or "truck" in lower:
        return "Truck"
    if "minivan" in lower or "van" in lower:
        return "Van"
    if "convertible" in lower or "cabriolet" in lower:
        return "Convertible"
    if "coupe" in lower:
        return "Coupe"
    if "hatchback" in lower:
        return "Hatchback"
    if "wagon" in lower or "estate" in lower:
        return "Wagon"
    if "sedan" in lower:
        return "Sedan"
    # Unmapped → passthrough (JS returns raw); truncate to fit varchar(20).
    return str(raw)[:_MAX_LEN]
