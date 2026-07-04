from __future__ import annotations
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.dealership import Dealership
from app.models.dealer_platform import DealerPlatform
from app.services.config_generator.generator import generate_config_for_dealership

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


@router.post('/{platform_id}/approve')
async def approve_config(
    platform_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail='Config not found')

    platform.status = 'active'
    platform.reviewed_at = datetime.utcnow()
    platform.reviewed_by = current_user.email

    session.add(platform)
    await session.commit()

    return {
        'message': f'Config {platform_id} approved and now active',
        'platform_id': platform_id,
        'platform_slug': platform.platform_slug,
        'source_url': platform.source_url,
    }


@router.post('/{platform_id}/reject')
async def reject_config(
    platform_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail='Config not found')

    platform.status = 'rejected'
    platform.reviewed_at = datetime.utcnow()
    platform.reviewed_by = current_user.email

    session.add(platform)
    await session.commit()
    return {'message': f'Config {platform_id} rejected'}
