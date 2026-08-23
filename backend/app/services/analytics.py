from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.ad_event import AdEvent
from app.models.user import User


async def record_api_usage(
    call_type: str,
    user_id: Optional[int] = None,
    quantity: int = 1,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    model: Optional[str] = None,
) -> None:
    """
    Fire-and-forget usage log for cost attribution (feeds usage_report + the
    future admin dashboard). Opens its own session and NEVER raises — a logging
    failure must not break the request it's measuring.
    """
    try:
        # Imported lazily to avoid circular imports at module load.
        from app.core.database import AsyncSessionLocal
        from app.models.api_usage import ApiUsage
        async with AsyncSessionLocal() as session:
            session.add(ApiUsage(
                call_type=call_type,
                user_id=user_id,
                quantity=quantity,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
            ))
            await session.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[usage] failed to record {call_type}: {e}")

PRELOADED_VOICE_IDS = {
    # English
    'Gubgw9l4dtIoQA9YZHgx', '3WqHLnw80rOZqJzW9YRB',
    'GZ4PpFJV8ikEGUtBrjK7', 'llNlEi50DSCIEuoOIaH7',
    'zDBYcuJrpuZ6YQ7AgRUw', 'C1npRmjB19a6yNkEucvx',
    'cPoqAvGWCPfCfyPMwe4z', 's3TPKV1kjDlVtZbl4Ksh',
    'tnSpp4vdxKPjI9w0GnoV', 'UgBBYS2sOqTuMpoF3BR0',
    'IjnA9kwZJHJ20Fp7Vmy6',
    # Spanish
    'zDMHo7CPscBTgfDtPOWl', 'G4IAP30yc6c1gK0csDfu',
    'k8cFOyAg7B9qwBlDDNTC', '9F4C8ztpNUmXkdDDbz3J',
    '8mBRP99B2Ng2QwsJMFQl', '22VndfJPBU7AZORAZZTT',
    'iqH5zmD4xxyGBHUsZ4Gt',
}


def _price_range(price_str: Optional[str]) -> Optional[str]:
    if not price_str:
        return None
    try:
        price = int(price_str.replace('$', '').replace(',', '').strip())
        if price < 15000:  return 'budget'
        if price < 30000:  return 'mid'
        if price < 50000:  return 'premium'
        return 'luxury'
    except Exception:
        return None


def _mileage_range(mileage_str: Optional[str]) -> Optional[str]:
    if not mileage_str:
        return None
    try:
        miles = int(mileage_str.replace(',', '').replace(' miles', '').strip())
        if miles < 30000:  return 'low'
        if miles < 80000:  return 'mid'
        return 'high'
    except Exception:
        return None


def _voice_type(voice_id: Optional[str]) -> Optional[str]:
    if not voice_id:
        return None
    return 'preloaded' if voice_id in PRELOADED_VOICE_IDS else 'custom'


async def track_generation(
    session:        AsyncSession,
    user:           User,
    job_id:         int,
    vehicle_data:   dict,
    video_format:   str,
    theme:          str,
    voice_id:       Optional[str],
    custom_script:  bool,
    photos_count:   int,
    render_seconds: int,
    succeeded:      bool,
    failure_reason: Optional[str] = None,
    language:       str = 'en',
) -> None:
    now = datetime.utcnow()

    event = AdEvent(
        user_id               = user.id,
        dealership_id         = user.dealership_id,
        event_type            = 'generated' if succeeded else 'generation_failed',
        job_id                = job_id,
        vehicle_year          = vehicle_data.get('year'),
        vehicle_make          = vehicle_data.get('make'),
        vehicle_model         = vehicle_data.get('model'),
        vehicle_price         = vehicle_data.get('price'),
        vehicle_price_range   = _price_range(vehicle_data.get('price')),
        vehicle_mileage_range = _mileage_range(vehicle_data.get('mileage')),
        language              = language,
        video_format          = video_format,
        theme_used            = theme,
        voice_id_used         = voice_id,
        voice_type            = _voice_type(voice_id),
        custom_script_used    = custom_script,
        photos_selected_count = photos_count,
        render_time_seconds   = render_seconds,
        failure_reason        = failure_reason,
        created_at            = now,
        day_of_week           = now.weekday(),
        hour_of_day           = now.hour,
    )

    session.add(event)
    await session.commit()
    print(f'Analytics: tracked generation for user {user.id}, job {job_id}')


async def track_posting(
    session:        AsyncSession,
    user:           User,
    event_type:     str,
    listing_id:     Optional[int],
    vehicle_data:   dict,
    job_created_at: Optional[datetime] = None,
    groups_count:   int = 0,
) -> None:
    now = datetime.utcnow()

    time_since_hours = None
    if job_created_at:
        delta = now - job_created_at.replace(tzinfo=timezone.utc)
        time_since_hours = round(delta.total_seconds() / 3600, 2)

    event = AdEvent(
        user_id                     = user.id,
        dealership_id               = user.dealership_id,
        event_type                  = event_type,
        listing_id                  = listing_id,
        vehicle_year                = vehicle_data.get('year'),
        vehicle_make                = vehicle_data.get('make'),
        vehicle_model               = vehicle_data.get('model'),
        vehicle_price               = vehicle_data.get('price'),
        vehicle_price_range         = _price_range(vehicle_data.get('price')),
        vehicle_mileage_range       = _mileage_range(vehicle_data.get('mileage')),
        groups_count                = groups_count if event_type == 'posted_fb_groups' else None,
        time_since_generation_hours = time_since_hours,
        created_at                  = now,
        day_of_week                 = now.weekday(),
        hour_of_day                 = now.hour,
    )

    session.add(event)
    await session.commit()
    print(f'Analytics: tracked {event_type} for user {user.id}')


async def track_sold(
    session:      AsyncSession,
    user:         User,
    listing_id:   int,
    vehicle_data: dict,
) -> None:
    now = datetime.utcnow()
    event = AdEvent(
        user_id               = user.id,
        dealership_id         = user.dealership_id,
        event_type            = 'sold_detected',
        listing_id            = listing_id,
        vehicle_year          = vehicle_data.get('year'),
        vehicle_make          = vehicle_data.get('make'),
        vehicle_model         = vehicle_data.get('model'),
        vehicle_price         = vehicle_data.get('price'),
        vehicle_price_range   = _price_range(vehicle_data.get('price')),
        created_at            = now,
        day_of_week           = now.weekday(),
        hour_of_day           = now.hour,
    )
    session.add(event)
    await session.commit()
