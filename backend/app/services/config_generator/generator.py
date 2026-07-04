'''
Ties scraper + claude_generator together.
Called as a FastAPI BackgroundTask during dealership signup flow.
'''
from __future__ import annotations
from .scraper import capture_platform_samples
from .claude_generator import generate_config


async def generate_config_for_dealership(inventory_url: str) -> dict:
    '''
    1. Playwright captures card + detail HTML
    2. Claude generates config JSON
    3. Returns config with metadata — caller saves to DB
    '''
    print(f'Config generator: starting for {inventory_url}')

    samples = await capture_platform_samples(inventory_url)
    config = await generate_config(
        samples['card_html'],
        samples['detail_html'],
    )

    config['_source'] = {
        'inventory_url': inventory_url,
        'detail_url': samples.get('detail_url'),
        'card_selector_matched': samples.get('card_selector_matched'),
    }
    config['_status'] = 'pending_review'
    return config
