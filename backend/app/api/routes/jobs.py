from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.core.middleware import require_active_subscription
from app.models.job import Job, JobCreate, JobRead, JobStatus
from app.models.user import User
from app.services.vin_decoder import decode_vin
from app.services.script_generator import generate_ad_script
from app.services.voice_clone import text_to_speech
from app.services.video_assembler import (
    build_ad_timeline_photo_only,
    submit_render,
    wait_for_render,
    wait_for_render_with_fallback,
    delete_render,
    download_render,
)
from app.services.s3 import (
    upload_bytes,
    make_audio_output_key,
    make_final_video_key,
    create_presigned_download_url,
    get_audio_duration,
)
from app.services.audio_utils import get_audio_level, calculate_volume_multiplier
from app.services.analytics import track_generation

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_active_subscription)],
)

# Separate router for Shotstack webhook — no auth, called by Shotstack directly
webhook_router = APIRouter(prefix="/jobs", tags=["jobs"])

BRIAN_VOICE_ID = "Gubgw9l4dtIoQA9YZHgx"
CLAUS_VOICE_ID = "zDMHo7CPscBTgfDtPOWl"

DEFAULT_CAR_PHOTOS = [
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/ZfmeNMBUffUiOHI44HeeZ-2eR0U.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/MmGE9KYnZW-P85anPOVCpjgH2sM.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/GulOJ7STWy3Yh637E3ORY1cpT7w.jpg",
]


async def _update_job(session: AsyncSession, job: Job, **kwargs) -> None:
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.updated_at = datetime.utcnow()
    session.add(job)
    await session.commit()
    await session.refresh(job)


async def _update_job_safe(job_id: int, **kwargs) -> None:
    """Open a fresh DB connection for each status update.

    The pipeline runs 5-15 minutes. Holding one session that long risks a
    dropped connection (AWS NAT gateway idle timeout is ~350 s). Each call
    here gets a fresh pooled connection, so pool_pre_ping catches any stale
    ones before they're used.
    """
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.updated_at = datetime.utcnow()
        session.add(job)
        await session.commit()


# ── Pipeline ──────────────────────────────────────────────────
async def _run_pipeline(job_id: int, user_id: int):
    """
    Ad generation pipeline — runs in the background.

    Stages:
      1. VIN decode         (10% → 30%)
      2. Script generation  (30% → 50%)
      3. Voice TTS          (50% → 70%)
      4. Video assembly     (70% → 100%)
    """
    from app.core.database import AsyncSessionLocal
    from app.core.config import get_settings
    from app.models.outro_video import OutroVideo
    settings = get_settings()

    # Fetch initial data then immediately release the session.
    # All subsequent DB writes use _update_job_safe (fresh connection each time).
    async with AsyncSessionLocal() as session:
        job  = await session.get(Job, job_id)
        user = await session.get(User, user_id)
        if not job or not user:
            return

        # ── Trial limit check ─────────────────────────────────
        if user.subscription_status == 'trial':
            now = datetime.now(timezone.utc)
            trial_end = user.trial_ends_at
            if trial_end is None:
                trial_end = now
            elif trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)

            if now > trial_end:
                await _update_job_safe(job_id,
                    status        = JobStatus.FAILED,
                    error_message = 'TRIAL_EXPIRED',
                    completed_at  = datetime.utcnow(),
                )
                return

            if user.trial_video_count >= 5:
                await _update_job_safe(job_id,
                    status        = JobStatus.FAILED,
                    error_message = 'TRIAL_VIDEO_LIMIT',
                    completed_at  = datetime.utcnow(),
                )
                return

        user_subscription_status    = user.subscription_status

        job_vin                     = job.vin
        job_theme                   = job.theme
        job_video_type              = job.video_type
        job_language                = job.language or 'en'
        job_custom_script           = job.custom_script
        job_car_photo_urls          = job.car_photo_urls
        job_price                   = job.price
        job_outro_video_id          = job.outro_video_id
        job_created_at              = job.created_at

        user_full_name              = user.full_name
        user_dealership_name        = user.dealership_name
        user_phone_number           = user.phone_number
        user_elevenlabs_voice_id    = user.elevenlabs_voice_id
        user_elevenlabs_voice_id_es = user.elevenlabs_voice_id_es

    print(f"Pipeline start — job_id={job_id}, vin={job_vin!r}, theme={job_theme!r}, video_type={job_video_type!r}, language={job_language!r}")

    # ── Stage 1: VIN decode ───────────────────────────────
    await _update_job_safe(job_id, status=JobStatus.VIN_DECODING, progress_pct=10)
    try:
        vehicle_data = await decode_vin(job_vin) if job_vin else {}

        # Sanity check: decoded VIN must match the job's VIN
        if job_vin and vehicle_data:
            decoded_vin = vehicle_data.get("vin", "").upper()
            if decoded_vin and decoded_vin != job_vin.upper():
                print(f"VIN MISMATCH — job.vin={job_vin!r}, decoded={decoded_vin!r}. Failing job.")
                await _update_job_safe(job_id,
                    status=JobStatus.FAILED,
                    error_message=(
                        f"VIN mismatch: submitted {job_vin} but NHTSA decoded {decoded_vin}. "
                        "This vehicle data may have been mixed up during import."
                    ),
                    completed_at=datetime.utcnow(),
                )
                return

        await _update_job_safe(job_id, vehicle_data=json.dumps(vehicle_data), progress_pct=30)
    except Exception as e:
        print(f"VIN decode failed (non-fatal): {e}")
        vehicle_data = {}
        await _update_job_safe(job_id, vehicle_data=json.dumps(vehicle_data), progress_pct=30)

    # ── Stage 2: Script generation ────────────────────────
    vd = vehicle_data if isinstance(vehicle_data, dict) else {}
    await _update_job_safe(job_id, status=JobStatus.SCRIPT_GENERATING, progress_pct=35)
    try:
        if job_custom_script:
            script = {
                "full_script": job_custom_script,
                "hook":        job_custom_script[:50],
                "body":        job_custom_script,
                "cta":         "",
                "theme":       "custom",
            }
            print(f"Using custom script: {job_custom_script[:50]}...")
        else:
            script = await generate_ad_script(
                vehicle_data=vd,
                theme=job_theme or "family",
                salesperson_name=user_full_name,
                dealership_name=None,
                phone_number=user_phone_number or None,
                include_cta=(job_video_type != "with_outro"),
                price=job_price or None,
                language=job_language,
            )
        await _update_job_safe(job_id, generated_script=json.dumps(script), progress_pct=50)
    except Exception as e:
        await _update_job_safe(job_id,
            status=JobStatus.FAILED,
            error_message=f"Script generation failed: {e}",
            completed_at=datetime.utcnow(),
        )
        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if user:
                    await track_generation(
                        session=session, user=user, job_id=job_id,
                        vehicle_data=vd, video_format=job_video_type or 'slideshow',
                        theme=job_theme or 'family', voice_id=user_elevenlabs_voice_id or BRIAN_VOICE_ID,
                        custom_script=bool(job_custom_script), photos_count=0,
                        render_seconds=0, succeeded=False, failure_reason=str(e),
                        language=job_language,
                    )
        except Exception:
            pass
        return

    # ── Stage 3: Voice TTS ────────────────────────────────
    if job_language == 'es':
        effective_voice_id = user_elevenlabs_voice_id_es or CLAUS_VOICE_ID
    else:
        effective_voice_id = user_elevenlabs_voice_id or BRIAN_VOICE_ID

    audio_s3_key = None
    await _update_job_safe(job_id, status=JobStatus.VOICE_CLONING, progress_pct=55)
    try:
        audio_bytes = await text_to_speech(
            text=script["full_script"],
            voice_id=effective_voice_id,
        )

        # Normalize voiceover loudness to -22 dBFS (quieter for video)
        try:
            from pydub import AudioSegment
            from pydub.effects import normalize
            import io as _io

            seg = AudioSegment.from_mp3(_io.BytesIO(audio_bytes))
            seg = normalize(seg)
            target_dBFS = -22.0
            seg = seg.apply_gain(target_dBFS - seg.dBFS)
            buf = _io.BytesIO()
            seg.export(buf, format="mp3", bitrate="192k")
            audio_bytes = buf.getvalue()
            print(f"Audio normalized to {target_dBFS} dBFS")
        except Exception as norm_err:
            print(f"Audio normalization failed (non-fatal): {norm_err}")

        audio_key = make_audio_output_key(job_id)
        upload_bytes(audio_bytes, audio_key, "audio/mpeg")
        audio_s3_key = audio_key
        await _update_job_safe(job_id, progress_pct=70)
    except Exception as e:
        await _update_job_safe(job_id,
            status=JobStatus.FAILED,
            error_message=f"Voice TTS failed: {e}",
            completed_at=datetime.utcnow(),
        )
        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if user:
                    await track_generation(
                        session=session, user=user, job_id=job_id,
                        vehicle_data=vd, video_format=job_video_type or 'slideshow',
                        theme=job_theme or 'family', voice_id=effective_voice_id,
                        custom_script=bool(job_custom_script), photos_count=0,
                        render_seconds=0, succeeded=False, failure_reason=str(e),
                        language=job_language,
                    )
        except Exception:
            pass
        return

    # ── Stage 4: Video assembly ───────────────────────────
    print(f"Stage 4 — video_type: {job_video_type}, audio_s3_key: {audio_s3_key}")
    if audio_s3_key:
        await _update_job_safe(job_id, status=JobStatus.ASSEMBLING, progress_pct=75)
        try:
            audio_url = (
                f"https://{settings.s3_bucket_name}.s3."
                f"{settings.aws_region}.amazonaws.com/{audio_s3_key}"
            )
            audio_duration = get_audio_duration(audio_s3_key)

            # Reviewed car photos
            car_photos = DEFAULT_CAR_PHOTOS
            if job_car_photo_urls:
                try:
                    car_photos = json.loads(job_car_photo_urls)
                except Exception:
                    car_photos = DEFAULT_CAR_PHOTOS

            try:
                from app.services.photo_classifier import get_walkaround_photos
                car_photos = await get_walkaround_photos(
                    photo_urls=car_photos,
                    exterior_count=5,
                    interior_count=2,
                )
                print(f"Walkaround photos: {len(car_photos)}")
            except Exception as e:
                print(f"Walkaround ordering failed: {e}")
                car_photos = car_photos[:7]

            highlights = _build_highlights(vd, user_dealership_name)
            from app.services.vin_decoder import vehicle_summary
            v_summary = vehicle_summary(vd) if vd else "Visit us today"

            # Fetch outro clip with a fresh session
            outro_url        = None
            outro_duration   = 8.0
            slideshow_volume = 1.0
            if job_video_type == "with_outro" and job_outro_video_id:
                async with AsyncSessionLocal() as session:
                    outro = await session.get(OutroVideo, job_outro_video_id)
                    if outro and outro.user_id == user_id:
                        outro_s3_key   = outro.s3_key
                        outro_url      = create_presigned_download_url(outro_s3_key, expires_in=3600)
                        outro_duration = outro.duration_seconds or 8.0
                        print(f"Outro: {outro.name}, {outro_duration}s")
                    else:
                        print("Outro not found — assembling slideshow only")
                        outro_s3_key = None

                if outro_url and outro_s3_key:
                    try:
                        outro_level = await get_audio_level(outro_s3_key)
                        if outro_level is not None:
                            voiceover_target = -22.0
                            slideshow_volume = calculate_volume_multiplier(outro_level, voiceover_target)
                            print(f"Audio balance: outro={outro_level:.1f}dBFS, slideshow_volume={slideshow_volume:.2f}")
                    except Exception as e:
                        print(f"Audio level measurement failed, using default: {e}")
                        slideshow_volume = 1.0

            timeline = build_ad_timeline_photo_only(
                audio_url=audio_url,
                car_photo_urls=car_photos,
                dealership_name=user_dealership_name,
                vehicle_summary=v_summary,
                feature_highlights=highlights,
                duration=audio_duration,
                brand_color="#C4122F",
                outro_video_url=outro_url,
                outro_duration=outro_duration,
                slideshow_volume=slideshow_volume,
                language=job_language,
            )

            render_id = await submit_render(timeline)

            # Store render_id so the webhook endpoint can look up this job
            await _update_job_safe(job_id,
                shotstack_render_id=render_id,
                status=JobStatus.ASSEMBLING,
                progress_pct=85,
            )
            print(f"Shotstack render submitted: {render_id} — waiting for webhook or fallback poll")

            fallback_url = await wait_for_render_with_fallback(render_id)

            if fallback_url:
                # Webhook didn't fire (dev, or missed) — fallback polling got the URL
                final_bytes = await download_render(fallback_url)
                final_key = make_final_video_key(job_id)
                upload_bytes(final_bytes, final_key, "video/mp4")
                presigned_url = create_presigned_download_url(final_key, expires_in=604800)

                try:
                    await delete_render(render_id)
                except Exception:
                    pass

                await _update_job_safe(job_id,
                    final_video_s3_key=final_key,
                    final_video_url=presigned_url,
                    progress_pct=100,
                )

        except Exception as e:
            await _update_job_safe(job_id,
                status=JobStatus.FAILED,
                error_message=f"Video assembly failed: {e}",
                completed_at=datetime.utcnow(),
            )
            try:
                async with AsyncSessionLocal() as session:
                    user = await session.get(User, user_id)
                    if user:
                        await track_generation(
                            session=session, user=user, job_id=job_id,
                            vehicle_data=vd, video_format=job_video_type or 'slideshow',
                            theme=job_theme or 'family', voice_id=effective_voice_id,
                            custom_script=bool(job_custom_script), photos_count=0,
                            render_seconds=0, succeeded=False, failure_reason=str(e),
                            language=job_language,
                        )
            except Exception:
                pass
            return

    # ── Track analytics ───────────────────────────────────────
    try:
        now         = datetime.utcnow()
        render_secs = int((now - job_created_at.replace(tzinfo=None)).total_seconds())
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if user:
                await track_generation(
                    session        = session,
                    user           = user,
                    job_id         = job_id,
                    vehicle_data   = vd,
                    video_format   = job_video_type or 'slideshow',
                    theme          = job_theme or 'family',
                    voice_id       = effective_voice_id,
                    custom_script  = bool(job_custom_script),
                    photos_count   = len(json.loads(job_car_photo_urls)) if job_car_photo_urls else 0,
                    render_seconds = render_secs,
                    succeeded      = True,
                    language       = job_language,
                )
    except Exception as e:
        print(f'Analytics tracking failed (non-fatal): {e}')

    # ── Done ─────────────────────────────────────────────
    await _update_job_safe(job_id,
        status=JobStatus.COMPLETED,
        progress_pct=100,
        completed_at=datetime.utcnow(),
    )

    # ── Increment trial video count ───────────────────────
    if user_subscription_status == 'trial':
        async with AsyncSessionLocal() as s:
            u = await s.get(User, user_id)
            if u:
                u.trial_video_count += 1
                s.add(u)
                await s.commit()
                print(f'Trial video count: {u.trial_video_count}/5')


@webhook_router.get("/webhook/shotstack")
async def shotstack_webhook_verify():
    """Shotstack sends a GET to validate the endpoint before POSTing events."""
    return {"ok": True}


@webhook_router.post("/webhook/shotstack")
async def shotstack_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Shotstack POSTs here when a render completes or fails.
    Returns 200 immediately (Shotstack retries if we take >10s) then
    processes the download/upload in a background task.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"received": True}

    print(f"Shotstack webhook received: {payload}")
    background_tasks.add_task(_process_shotstack_webhook, payload)
    return {"received": True}


async def _process_shotstack_webhook(payload: dict):
    # Payload shape (flat): id, status, url, error, type, action, owner, completed
    render_id = payload.get("id")
    status    = payload.get("status")
    video_url = payload.get("url")
    error     = payload.get("error")

    if not render_id:
        print("Shotstack webhook: no render_id in payload")
        return

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        result = await session.exec(
            select(Job).where(Job.shotstack_render_id == render_id)
        )
        job = result.first()

    if not job:
        print(f"Shotstack webhook: no job found for render_id {render_id}")
        return

    if status == "done":
        if not video_url:
            print(f"Shotstack webhook: done but no url for render {render_id}")
            return
        try:
            final_bytes   = await download_render(video_url)
            final_key     = make_final_video_key(job.id)
            upload_bytes(final_bytes, final_key, "video/mp4")
            presigned_url = create_presigned_download_url(final_key, expires_in=604800)

            try:
                await delete_render(render_id)
                print(f"Deleted render {render_id} from Shotstack")
            except Exception:
                pass

            await _update_job_safe(job.id,
                status             = JobStatus.COMPLETED,
                final_video_s3_key = final_key,
                final_video_url    = presigned_url,
                progress_pct       = 100,
                completed_at       = datetime.utcnow(),
            )
            print(f"Job {job.id} completed via webhook")

        except Exception as e:
            print(f"Shotstack webhook: post-render processing failed: {e}")
            await _update_job_safe(job.id,
                status        = JobStatus.FAILED,
                error_message = f"Post-render processing failed: {e}",
                completed_at  = datetime.utcnow(),
            )

    elif status == "failed":
        print(f"Job {job.id} failed via webhook: {error}")
        await _update_job_safe(job.id,
            status        = JobStatus.FAILED,
            error_message = f"Video assembly failed: {error or 'Shotstack render failed'}",
            completed_at  = datetime.utcnow(),
        )


def _build_highlights(vd: dict, dealership_name: str) -> list[str]:
    highlights = []
    drive = vd.get("drivetrain") or ""
    if drive:
        if "all-wheel" in drive.lower() or "awd" in drive.lower():
            highlights.append("All-Wheel Drive")
        elif "4wd" in drive.lower() or "four-wheel" in drive.lower():
            highlights.append("4-Wheel Drive")
        elif "rear" in drive.lower() or "rwd" in drive.lower():
            highlights.append("Rear-Wheel Drive")
        else:
            highlights.append(drive)
    engine = vd.get("engine") or ""
    if engine:
        highlights.append(engine)
    transmission = vd.get("transmission") or ""
    if "automatic" in transmission.lower():
        highlights.append("Automatic Transmission")
    elif "manual" in transmission.lower():
        highlights.append("Manual Transmission")
    body = vd.get("body_style") or ""
    if body and len(highlights) < 3:
        highlights.append(body)
    fuel = vd.get("fuel_type") or ""
    if fuel and len(highlights) < 3:
        if "electric" in fuel.lower():
            highlights.append("Electric Vehicle")
        elif "hybrid" in fuel.lower():
            highlights.append("Hybrid")
    highlights = highlights[:3]
    while len(highlights) < 3:
        highlights.append(dealership_name)
    return highlights


# ── Create job ────────────────────────────────────────────────
@router.post("/", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create a new ad generation job. Returns immediately — poll GET /jobs/{id} for progress."""
    if not payload.vin and not payload.listing_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please provide either a VIN or a listing URL.",
        )

    job = Job(
        user_id=current_user.id,
        vin=payload.vin,
        listing_url=payload.listing_url,
        theme=payload.theme,
        custom_script=payload.custom_script or None,
        price=payload.price or None,
        video_type=payload.video_type,
        outro_video_id=payload.outro_video_id,
        photos_s3_keys=payload.photos_s3_keys,
        voice_s3_key=payload.voice_s3_key,
        car_photo_urls=payload.car_photo_urls,
        language=payload.language or 'en',
        status=JobStatus.PENDING,
        progress_pct=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    background_tasks.add_task(_run_pipeline, job_id=job.id, user_id=current_user.id)
    return job


# ── Get single job ────────────────────────────────────────────
@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Poll a job's current status and progress."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")
    return job


# ── List jobs ─────────────────────────────────────────────────
@router.get("/", response_model=list[JobRead])
async def list_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 20,
    offset: int = 0,
):
    """Return the current user's job history, newest first."""
    result = await session.exec(
        select(Job)
        .where(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()
