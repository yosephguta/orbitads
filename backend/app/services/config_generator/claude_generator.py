'''
Feeds HTML captured by scraper.py to Claude and returns a JSON config
matching the DEALERSHIP_CONFIGS schema in dealership_config.js.
'''
from __future__ import annotations
import json
import re
from anthropic import AsyncAnthropic
from app.core.config import get_settings

MODEL = 'claude-sonnet-4-6'

SYSTEM_PROMPT = '''You are configuring a web scraper for a car dealership inventory website.
You will receive raw HTML: one vehicle card from an inventory listing page,
and fragments from that vehicle's detail page (price section, spec table, gallery).

Output ONLY a valid JSON object — no markdown, no prose, no code fences.

Schema:
{
  "platform": "<short slug you infer, e.g. 'dealer.com', 'cdk', 'dealersocket', 'dealerinspire'>",
  "inventory": {
    "vehicle_cards": "<CSS selector matching the repeating card container>",
    "url_pattern": "<URL path pattern identifying inventory pages, e.g. '/used-inventory/'>",
    "data_attribute": "<name of data-* attribute holding JSON vehicle data blob, or null>",
    "fields": {
      "year":        "<CSS selector for year text, or null>",
      "make":        "<CSS selector for make text, or null>",
      "model":       "<CSS selector for model text, or null>",
      "trim":        "<CSS selector for trim text, or null>",
      "price":       "<CSS selector for FINAL ADVERTISED PRICE ONLY — never MSRP/retail>",
      "mileage":     "<CSS selector for mileage text, or null>",
      "vin":         "<CSS selector for VIN, or null>",
      "listing_url": "<CSS selector for link to detail page>",
      "image":       "<CSS selector for card thumbnail img>"
    },
    "button_injection": "<CSS selector for good Import button injection point>"
  },
  "detail_page": {
    "sale_price":       "<CSS selector for the FINAL ADVERTISED PRICE — must include all mandatory dealer fees (processing fee, doc fee, dealer fee). If the site shows a base price + separate processing/doc fee, use the combined total. Labels to look for: 'Internet Price', 'Our Price', 'Online Price', 'Final Price', '[Dealer Name] Price'. NEVER use MSRP, Retail Value, or a pre-fee base price.>",
    "retail_price":     "<CSS selector for MSRP/Retail Value if shown separately, else null>",
    "processing_fee":   "<CSS selector for the processing/doc/dealer fee line if shown separately, else null>",
    "exterior_color":   "<CSS selector>",
    "interior_color":   "<CSS selector>",
    "body_style":       "<CSS selector, or null>",
    "mileage":          "<CSS selector>",
    "vin":              "<CSS selector>",
    "photos":           "<CSS selector matching each photo img — note if URL is in src or data-src>"
  },
  "sold_indicators": [
    "<strings that appear in page HTML/JS when vehicle is sold or removed>"
  ],
  "photo_hints": {
    "exterior_count": null,
    "interior_start": null
  },
  "notes_for_human_review": [
    "<any field you were NOT confident about>",
    "<anything needing a second sample vehicle to confirm>",
    "<sold_indicators usually need manual verification — flag this>"
  ]
}

Critical rules:
- For price/sale_price on inventory cards and detail pages: ALWAYS use the FINAL
  ADVERTISED price that includes all mandatory dealer fees (processing fee, doc fee,
  dealer fee). Dealers are legally required to advertise the total price including
  mandatory fees. Look for labels like "Internet Price", "Our Price", "Online Price",
  "[Dealer Name] Price", "Final Price". If you see a base "Sale Price" AND a separate
  "Processing Fee" or "Doc Fee", the sale_price selector should target whichever
  element shows their SUM (often labeled "Total" or "[Dealer] Price"). The processing
  fee amount varies by state and dealer — never hardcode a dollar amount.
  NEVER select MSRP, Retail Value, or a pre-fee base price — this is an FTC
  compliance requirement.
- If a data-* attribute contains JSON with vehicle fields, set data_attribute
  and still fill in CSS selectors as fallback.
- Prefer stable class/data-testid selectors over positional selectors.
- If not confident about a field, set it to null and explain in notes_for_human_review.
- Output valid JSON only — no exceptions.'''


async def generate_config(card_html: str, detail_html: str | None) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_content = f'VEHICLE CARD HTML:\n{card_html}\n\n'
    if detail_html:
        user_content += f'DETAIL PAGE HTML FRAGMENTS:\n{detail_html}'
    else:
        user_content += 'DETAIL PAGE HTML FRAGMENTS: (not available)'

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_content}],
    )

    raw = ''.join(b.text for b in response.content if b.type == 'text')
    raw = re.sub(r'^```json\s*|\s*```$', '', raw.strip())

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f'Claude did not return valid JSON: {e}\nRaw: {raw[:500]}'
        )

    warnings = []
    price_sel = config.get('inventory', {}).get('fields', {}).get('price', '') or ''
    if any(bad in price_sel.lower() for bad in ['msrp', 'retail', 'starting']):
        warnings.append(
            'price selector may target MSRP/retail instead of sale price — verify manually'
        )
    if not config.get('inventory', {}).get('vehicle_cards'):
        warnings.append('vehicle_cards selector missing — config is unusable without this')
    if not config.get('sold_indicators'):
        warnings.append(
            'sold_indicators empty — these must be verified manually on a sold vehicle page'
        )

    config['_generation_warnings'] = warnings
    config['_usage'] = {
        'input_tokens': response.usage.input_tokens,
        'output_tokens': response.usage.output_tokens,
    }
    return config
