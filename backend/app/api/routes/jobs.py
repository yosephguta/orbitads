from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
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
    download_render,
)
from app.services.s3 import (
    upload_bytes,
    make_audio_output_key,
    make_final_video_key,
    create_presigned_download_url,
    get_audio_duration,
)

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_active_subscription)],
)

DEFAULT_CAR_PHOTOS = [
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/ZfmeNMBUffUiOHI44HeeZ-2eR0U.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/MmGE9KYnZW-P85anPOVCpjgH2sM.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/GulOJ7STWy3Yh637E3ORY1cpT7w.jpg",
]


async def _update_job(session: AsyncSession, job: Job, **kwargs) -> None:
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    await session.commit()
    await session.refresh(job)


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

    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        user = await session.get(User, user_id)

        if not job or not user:
            return

        print(f"Pipeline start — job_id={job_id}, vin={job.vin!r}, theme={job.theme!r}, video_type={job.video_type!r}")

        # ── Stage 1: VIN decode ───────────────────────────────
        await _update_job(session, job, status=JobStatus.VIN_DECODING, progress_pct=10)
        try:
            vehicle_data = await decode_vin(job.vin) if job.vin else {}

            # Sanity check: decoded VIN must match the job's VIN
            if job.vin and vehicle_data:
                decoded_vin = vehicle_data.get("vin", "").upper()
                if decoded_vin and decoded_vin != job.vin.upper():
                    print(f"VIN MISMATCH — job.vin={job.vin!r}, decoded={decoded_vin!r}. Failing job.")
                    await _update_job(session, job,
                        status=JobStatus.FAILED,
                        error_message=(
                            f"VIN mismatch: submitted {job.vin} but NHTSA decoded {decoded_vin}. "
                            "This vehicle data may have been mixed up during import."
                        ),
                        completed_at=datetime.now(timezone.utc),
                    )
                    return

            await _update_job(session, job, vehicle_data=json.dumps(vehicle_data), progress_pct=30)
        except Exception as e:
            print(f"VIN decode failed (non-fatal): {e}")
            vehicle_data = {}
            await _update_job(session, job, vehicle_data=json.dumps(vehicle_data), progress_pct=30)

        # ── Stage 2: Script generation ────────────────────────
        vd = vehicle_data if isinstance(vehicle_data, dict) else {}
        await _update_job(session, job, status=JobStatus.SCRIPT_GENERATING, progress_pct=35)
        try:
            if job.custom_script:
                script = {
                    "full_script": job.custom_script,
                    "hook":        job.custom_script[:50],
                    "body":        job.custom_script,
                    "cta":         "",
                    "theme":       "custom",
                }
                print(f"Using custom script: {job.custom_script[:50]}...")
            else:
                script = await generate_ad_script(
                    vehicle_data=vd,
                    theme=job.theme or "family",
                    salesperson_name=user.full_name,
                    dealership_name=None,
                    phone_number=user.phone_number or None,
                    include_cta=(job.video_type != "with_outro"),
                )
            await _update_job(session, job, generated_script=json.dumps(script), progress_pct=50)
        except Exception as e:
            await _update_job(session, job,
                status=JobStatus.FAILED,
                error_message=f"Script generation failed: {e}",
                completed_at=datetime.now(timezone.utc),
            )
            return

        # ── Stage 3: Voice TTS ────────────────────────────────
        audio_s3_key = None
        if user.elevenlabs_voice_id:
            await _update_job(session, job, status=JobStatus.VOICE_CLONING, progress_pct=55)
            try:
                audio_bytes = await text_to_speech(
                    text=script["full_script"],
                    voice_id=user.elevenlabs_voice_id,
                )
                audio_key = make_audio_output_key(job.id)
                upload_bytes(audio_bytes, audio_key, "audio/mpeg")
                audio_s3_key = audio_key
                await _update_job(session, job, progress_pct=70)
            except Exception as e:
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Voice TTS failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Stage 4: Video assembly ───────────────────────────
        print(f"Stage 4 — video_type: {job.video_type}, audio_s3_key: {audio_s3_key}")
        if audio_s3_key:
            await _update_job(session, job, status=JobStatus.ASSEMBLING, progress_pct=75)
            try:
                audio_url = (
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{audio_s3_key}"
                )
                audio_duration = get_audio_duration(audio_s3_key)

                # Reviewed car photos
                car_photos = DEFAULT_CAR_PHOTOS
                if job.car_photo_urls:
                    try:
                        car_photos = json.loads(job.car_photo_urls)
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

                vd = json.loads(job.vehicle_data) if job.vehicle_data else {}
                highlights = _build_highlights(vd, user.dealership_name)
                from app.services.vin_decoder import vehicle_summary
                v_summary = vehicle_summary(vd) if vd else "Visit us today"

                # Fetch outro clip when requested
                outro_url = None
                outro_duration = 8.0
                if job.video_type == "with_outro" and job.outro_video_id:
                    outro = await session.get(OutroVideo, job.outro_video_id)
                    if outro and outro.user_id == user_id:
                        outro_url = create_presigned_download_url(outro.s3_key, expires_in=3600)
                        outro_duration = outro.duration_seconds or 8.0
                        print(f"Outro: {outro.name}, {outro_duration}s")
                    else:
                        print("Outro not found — assembling slideshow only")

                timeline = build_ad_timeline_photo_only(
                    audio_url=audio_url,
                    car_photo_urls=car_photos,
                    dealership_name=user.dealership_name,
                    vehicle_summary=v_summary,
                    feature_highlights=highlights,
                    duration=audio_duration,
                    brand_color="#C4122F",
                    outro_video_url=outro_url,
                    outro_duration=outro_duration,
                )

                render_id = await submit_render(timeline)
                final_video_url = await wait_for_render(render_id)
                final_bytes = await download_render(final_video_url)
                final_key = make_final_video_key(job.id)
                upload_bytes(final_bytes, final_key, "video/mp4")
                presigned_url = create_presigned_download_url(final_key, expires_in=604800)

                await _update_job(session, job,
                    final_video_s3_key=final_key,
                    final_video_url=presigned_url,
                    progress_pct=100,
                )

            except Exception as e:
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Video assembly failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Done ─────────────────────────────────────────────
        await _update_job(session, job,
            status=JobStatus.COMPLETED,
            progress_pct=100,
            completed_at=datetime.now(timezone.utc),
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
        video_type=payload.video_type,
        outro_video_id=payload.outro_video_id,
        photos_s3_keys=payload.photos_s3_keys,
        voice_s3_key=payload.voice_s3_key,
        car_photo_urls=payload.car_photo_urls,
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
