'''
Playwright scraper for dealership inventory pages.
Captures card HTML + detail page HTML sample for Claude config generation.
Runs headless in the backend at signup time.
'''
from __future__ import annotations
import asyncio
import re
from playwright.async_api import async_playwright, Page

# Ordered from most specific to most generic.
# Covers: Dealer Inspire, Dealer.com/Cox, CDK Global, DealerSocket,
# EDealer, Sincro, and common generic patterns.
CANDIDATE_CARD_SELECTORS = [
    # Data-attribute driven (most reliable)
    '[data-vehicle]',
    '[data-vehicle-details]',
    '[data-vehicle-id]',
    '[data-listing-id]',
    '[data-vin]',
    # Specific platform classes
    '.vehicle-card',
    '.inventory-listing-item',
    '.result-wrap',
    '.srp-list-item',
    '.vehicle-item',
    '.listing-item',
    '.inventory-item',
    '.inv-item',
    # Cox Automotive / Dealer.com
    '.ddc-item-info-wrapper',
    '[class*="ddc-item"]',
    # CDK Global
    '.inventory-listing',
    '[class*="inventory-listing"]',
    # DealerSocket / Sincro
    '.vehicle-listing-item',
    '[class*="VehicleCard"]',
    '[class*="vehicle-result"]',
    # Broad class substring matches
    '[class*="vehicle-card"]',
    '[class*="inventory-item"]',
    '[class*="vehicle-listing"]',
    '[class*="listing-item"]',
    '[class*="vehicle-tile"]',
    '[class*="car-card"]',
    # Element + class combinations
    'article[class*="vehicle"]',
    'article[class*="card"]',
    'li[class*="vehicle"]',
    'li[class*="inventory"]',
    'div[class*="vehicle-card"]',
]


def _trim_gallery_html(html: str, max_images: int = 3) -> str:
    slide_pattern = re.compile(
        r'(<(?:div|li)[^>]*class="[^"]*(?:swiper-slide|gallery-slide|photo-slide)[^"]*".*?</(?:div|li)>)',
        re.DOTALL | re.IGNORECASE,
    )
    slides = slide_pattern.findall(html)
    if len(slides) > max_images:
        for extra in slides[max_images:]:
            html = html.replace(extra, '', 1)
    return html


async def _find_first_card(page: Page):
    for selector in CANDIDATE_CARD_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count >= 2:  # Require at least 2 to avoid nav/header false positives
                print(f'Scraper: found {count} cards with selector: {selector}')
                return locator.first, selector
        except Exception:
            continue
    return None, None


async def _trigger_lazy_load(page: Page) -> None:
    '''Scroll down and back up to trigger lazy-loaded content.'''
    try:
        await page.evaluate('''() => {
            window.scrollTo(0, document.body.scrollHeight / 2);
        }''')
        await asyncio.sleep(1)
        await page.evaluate('() => { window.scrollTo(0, document.body.scrollHeight); }')
        await asyncio.sleep(1)
        await page.evaluate('() => { window.scrollTo(0, 0); }')
    except Exception:
        pass


async def capture_platform_samples(
    inventory_url: str,
    timeout_ms: int = 30000,
) -> dict:
    '''
    Navigate to dealership inventory page, find first vehicle card,
    follow link to detail page, return HTML samples for Claude.
    '''
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        try:
            page = await browser.new_page(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0 Safari/537.36'
                )
            )

            print(f'Scraper: navigating to {inventory_url}')

            # Try networkidle first; fall back to load for SPAs that never go idle
            try:
                await page.goto(inventory_url, wait_until='networkidle', timeout=timeout_ms)
            except Exception:
                print('Scraper: networkidle timed out, retrying with load event')
                await page.goto(inventory_url, wait_until='load', timeout=timeout_ms)

            # Initial wait + scroll to trigger lazy-loaded cards
            await asyncio.sleep(3)
            await _trigger_lazy_load(page)
            await asyncio.sleep(2)

            card_locator, matched_selector = await _find_first_card(page)

            if card_locator is None:
                raise RuntimeError(
                    f'No vehicle card found at {inventory_url}. '
                    'The site may need JavaScript rendering time or '
                    'uses selectors not in our heuristic list.'
                )

            card_html = await card_locator.evaluate('el => el.outerHTML')
            print(f'Scraper: captured card HTML ({len(card_html)} chars)')

            detail_href = await card_locator.evaluate('''el => {
                const patterns = ['/used/', '/inventory/', '/vehicle', '/vdp/', '/cars/', '/used-'];
                for (const pattern of patterns) {
                    const a = el.querySelector('a[href*="' + pattern + '"]');
                    if (a && a.href) return a.href;
                }
                // Fallback: any link in the card that isn't a nav/utility link
                const links = Array.from(el.querySelectorAll('a[href]'));
                const detail = links.find(a =>
                    a.href.includes(window.location.hostname) &&
                    !a.href.includes('#') &&
                    a.href !== window.location.href
                );
                return detail ? detail.href : null;
            }''')

            detail_html = None
            detail_url = None

            if detail_href:
                detail_url = detail_href
                detail_page = await browser.new_page()

                try:
                    print(f'Scraper: navigating to detail page {detail_href}')
                    try:
                        await detail_page.goto(
                            detail_href, wait_until='networkidle', timeout=timeout_ms
                        )
                    except Exception:
                        await detail_page.goto(
                            detail_href, wait_until='load', timeout=timeout_ms
                        )
                    await asyncio.sleep(3)

                    price_selectors = [
                        '#price-box', '.vdp-price-box', '.price-box',
                        '.pricing-detail', '[class*="pricing"]',
                        '.vehicle-pricing', '.price-section',
                        'dl.pricing-detail', '[class*="price-block"]',
                        '[class*="priceBlock"]', '[class*="price-box"]',
                    ]
                    spec_selectors = [
                        '.basic-info-component', '.vehicle-details',
                        '.specs-table', '[class*="spec"]',
                        'dl.dl-horizontal', '.vehicle-info',
                        '.vehicle-specs', '[class*="vehicle-info"]',
                        '[class*="features"]', '.attributes',
                    ]
                    gallery_selectors = [
                        '.vdp-gallery', '.media-gallery',
                        '[class*="gallery"]', '.vehicle-photos',
                        '.photo-gallery', '[class*="photo-viewer"]',
                        '[class*="image-viewer"]', '[class*="slider"]',
                    ]

                    fragments = []

                    for sel in price_selectors:
                        loc = detail_page.locator(sel)
                        if await loc.count() > 0:
                            html = await loc.first.evaluate('el => el.outerHTML')
                            fragments.append(f'<!-- PRICE SECTION -->\n{html}')
                            break

                    for sel in spec_selectors:
                        loc = detail_page.locator(sel)
                        if await loc.count() > 0:
                            html = await loc.first.evaluate('el => el.outerHTML')
                            fragments.append(f'<!-- SPEC SECTION -->\n{html}')
                            break

                    for sel in gallery_selectors:
                        loc = detail_page.locator(sel)
                        if await loc.count() > 0:
                            html = await loc.first.evaluate('el => el.outerHTML')
                            fragments.append(
                                f'<!-- GALLERY (trimmed) -->\n{_trim_gallery_html(html)}'
                            )
                            break

                    detail_html = '\n\n'.join(fragments) if fragments else None
                    print(f'Scraper: captured detail HTML ({len(detail_html or "")} chars)')

                finally:
                    await detail_page.close()

            return {
                'card_html': card_html,
                'card_selector_matched': matched_selector,
                'detail_html': detail_html,
                'detail_url': detail_url,
            }

        finally:
            await browser.close()
