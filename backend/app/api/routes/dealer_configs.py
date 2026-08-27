from __future__ import annotations
from datetime import datetime
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.dealership import Dealership
from app.models.dealer_platform import DealerPlatform
from app.services.config_generator.generator import generate_config_for_dealership
from app.services.config_generator.claude_generator import generate_config, generate_config_with_hints

router = APIRouter(prefix='/dealer-configs', tags=['dealer-configs'])


async def _run_config_generation(
    dealership_id: int,
    inventory_url: str,
    session_factory,
):
    try:
        print(f'Background config gen starting for dealership {dealership_id}')
        config = await generate_config_for_dealership(inventory_url)

        platform_slug = config.get('platform', 'unknown')

        async with session_factory() as session:
            platform = DealerPlatform(
                name=f'Auto-generated: {platform_slug}',
                platform_slug=platform_slug,
                config_json=config,
                status='pending_review',
                source_url=inventory_url,
                notes='\n'.join(config.get('notes_for_human_review', [])),
                generation_warnings=config.get('_generation_warnings', []),
                input_tokens=config.get('_usage', {}).get('input_tokens'),
                output_tokens=config.get('_usage', {}).get('output_tokens'),
            )
            session.add(platform)
            await session.commit()
            await session.refresh(platform)

            dealership = await session.get(Dealership, dealership_id)
            if dealership:
                dealership.platform_id = platform.id
                session.add(dealership)
                await session.commit()

            print(f'Config saved: DealerPlatform id={platform.id}, status=pending_review')

    except Exception as e:
        print(f'Config generation failed for dealership {dealership_id}: {e}')


@router.post('/dealerships/{dealership_id}/auto-configure')
async def auto_configure_dealership(
    dealership_id: int,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail='Dealership not found')

    inventory_url = (
        dealership.website_url
        if dealership.website_url
        else f'https://{dealership.dealership_name.lower().replace(" ", "")}.com/used-inventory/'
    )

    from app.core.database import AsyncSessionLocal
    background_tasks.add_task(
        _run_config_generation,
        dealership_id,
        inventory_url,
        AsyncSessionLocal,
    )

    return {
        'message': 'Config generation started in background',
        'dealership_id': dealership_id,
        'inventory_url': inventory_url,
        'status': 'generating — check /dealer-configs/pending in 30-60 seconds',
    }


@router.get('/pending')
async def list_pending_configs(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await session.exec(
        select(DealerPlatform)
        .where(DealerPlatform.status == 'pending_review')
        .order_by(DealerPlatform.created_at.desc())
    )
    platforms = result.all()
    return {
        'count': len(platforms),
        'configs': [
            {
                'id': p.id,
                'name': p.name,
                'platform_slug': p.platform_slug,
                'source_url': p.source_url,
                'status': p.status,
                'warnings': p.generation_warnings,
                'notes': p.notes,
                'created_at': p.created_at,
                'config_preview': {
                    'vehicle_cards': p.config_json.get('inventory', {}).get('vehicle_cards'),
                    'sale_price': p.config_json.get('detail_page', {}).get('sale_price'),
                    'platform': p.config_json.get('platform'),
                },
            }
            for p in platforms
        ],
    }


class GenerateFromHtmlRequest(BaseModel):
    source_url:              str
    card_html:               Optional[str] = None
    detail_html:             Optional[str] = None
    card_selector:           Optional[str] = None
    selected_price:          Optional[dict] = None
    exterior_photo_selector: Optional[str] = None
    interior_photo_selector: Optional[str] = None
    for_new_cars:            bool = False


@router.post('/generate-from-html')
async def generate_config_from_html(
    payload: GenerateFromHtmlRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    '''
    Extension-side config generation. Claude generates a config saved as
    pending_review for manual admin approval. When card_selector / selected_price
    are provided (new interactive onboarding), those values are seeded directly
    into the config — Claude only fills in the remaining unknowns.
    '''
    source_domain = (
        payload.source_url
        .replace('https://', '').replace('http://', '').replace('www.', '')
        .split('/')[0]
    )

    # Block if an active config already exists — return it so extension can use it immediately
    existing_active_result = await session.exec(
        select(DealerPlatform).where(
            DealerPlatform.source_url.contains(source_domain),
            DealerPlatform.status == 'active',
        )
    )
    existing_active = existing_active_result.first()
    if existing_active:
        raise HTTPException(
            status_code=409,
            detail={
                'message': 'An active config for this domain already exists.',
                'config_id': existing_active.id,
                'config': existing_active.config_json,
            }
        )

    # Build known-selectors dict from user interaction data
    known_selectors = {}
    if payload.card_selector:
        known_selectors['vehicle_cards'] = payload.card_selector
    if payload.selected_price:
        known_selectors['sale_price_label']    = payload.selected_price.get('label')
        known_selectors['sale_price_value']    = payload.selected_price.get('value')
        known_selectors['sale_price_selector'] = payload.selected_price.get('selector')
    if payload.exterior_photo_selector:
        known_selectors['exterior_photo_selector'] = payload.exterior_photo_selector
    if payload.interior_photo_selector:
        known_selectors['interior_photo_selector'] = payload.interior_photo_selector

    try:
        if known_selectors:
            config = await generate_config_with_hints(
                detail_html=payload.detail_html,
                known_selectors=known_selectors,
                source_url=payload.source_url,
                for_new_cars=payload.for_new_cars,
            )
        else:
            config = await generate_config(payload.card_html or '', payload.detail_html)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f'Config generation failed: {e}')

    platform_slug = config.get('platform', 'unknown')
    notes_lines   = list(config.get('notes_for_human_review', []))
    if payload.card_selector:
        notes_lines.insert(0, f'[Interactive] card_selector confirmed by user click: {payload.card_selector}')
    if payload.selected_price:
        notes_lines.insert(0, f'[Interactive] price confirmed by user: {payload.selected_price.get("label")} = {payload.selected_price.get("value")}')

    platform = DealerPlatform(
        name=f'Auto-generated: {platform_slug}',
        platform_slug=platform_slug,
        config_json=config,
        status='pending_review',
        source_url=payload.source_url,
        notes='\n'.join(notes_lines),
        generation_warnings=config.get('_generation_warnings', []),
        input_tokens=config.get('_usage', {}).get('input_tokens'),
        output_tokens=config.get('_usage', {}).get('output_tokens'),
    )
    session.add(platform)
    await session.commit()
    await session.refresh(platform)

    print(f'Config generated from HTML: DealerPlatform id={platform.id} for {payload.source_url}')

    return {
        'config_id': platform.id,
        'config':    config,
        'status':    'pending_review',
        'warnings':  config.get('_generation_warnings', []),
    }


@router.get('/domain/{domain}')
async def get_config_for_domain(
    domain: str,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    result = await session.exec(
        select(DealerPlatform).where(DealerPlatform.status == 'active')
    )
    platforms = result.all()

    for platform in platforms:
        source_domain = (
            platform.source_url
            .replace('https://', '')
            .replace('http://', '')
            .split('/')[0]
        )
        if domain == source_domain or domain.endswith(source_domain):
            return {
                'found': True,
                'config': platform.config_json,
                'platform_id': platform.id,
                'platform': platform.platform_slug,
            }

    return {'found': False, 'config': None}


@router.get('/{platform_id}')
async def get_config(
    platform_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail='Config not found')
    return platform


# NOTE: approve / reject moved to admin.py under /admin/dealer-platforms/* and
# re-gated behind get_current_admin (Part 3). They were previously here gated by
# get_current_user — i.e. ANY logged-in user could activate a scraping config,
# which is the security gap Part 3 closes. The review-queue list also lives
# there now (GET /admin/dealer-platforms?status=...). This file keeps only the
# extension-facing / generation endpoints.


@router.post('/{platform_id}/flag-manual')
async def flag_for_manual_review(
    platform_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    platform = await session.get(DealerPlatform, platform_id)
    if platform:
        platform.notes = (platform.notes or '') + \
            '\n⚠️ FLAGGED: User reported incorrect scraping — priority manual review needed'
        session.add(platform)
        await session.commit()
    return {'flagged': True}
