from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.job import Job, JobCreate, JobRead, JobStatus
from app.models.user import User
from app.services.vin_decoder import decode_vin
from app.services.script_generator import generate_ad_script
from app.services.voice_clone import text_to_speech
from app.services.avatar import submit_video, wait_for_video, download_video
from app.services.video_assembler import (
    build_ad_timeline,
    submit_render,
    wait_for_render,
    download_render,
)
from app.services.s3 import (
    upload_bytes,
    make_audio_output_key,
    make_avatar_output_key,
    make_final_video_key,
    create_presigned_download_url,
    get_audio_duration,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Default car photos used when none are provided
# These are placeholder Kia photos — Step 18 replaces this with real scraping
DEFAULT_CAR_PHOTOS = [
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/ZfmeNMBUffUiOHI44HeeZ-2eR0U.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/MmGE9KYnZW-P85anPOVCpjgH2sM.jpg",
    "https://platform.cstatic-images.com/xxlarge/in/v2/ff3aaaec-e513-4b42-8f96-8ed9d9280fd1/0b4af000-a573-4afc-b04d-9c9639bdbf02/GulOJ7STWy3Yh637E3ORY1cpT7w.jpg",
]


# ── Helper ────────────────────────────────────────────────────
async def _update_job(session: AsyncSession, job: Job, **kwargs) -> None:
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    await session.commit()
    await session.refresh(job)


# ── Full pipeline ─────────────────────────────────────────────
async def _run_pipeline(job_id: int, user_id: int):
    """
    Full ad generation pipeline — runs in the background.

    Stages:
      1. VIN decode         (10% → 30%)
      2. Script generation  (30% → 50%)
      3. Voice TTS          (50% → 65%)
      4. Avatar generation  (65% → 80%)
      5. Video assembly     (80% → 100%)
    """
    from app.core.database import AsyncSessionLocal
    from app.core.config import get_settings
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        user = await session.get(User, user_id)

        if not job or not user:
            return

        # ── Stage 1: VIN decode ───────────────────────────────
        await _update_job(session, job,
            status=JobStatus.VIN_DECODING,
            progress_pct=10,
        )
        try:
            vehicle_data = await decode_vin(job.vin) if job.vin else {}
            await _update_job(session, job,
                vehicle_data=json.dumps(vehicle_data),
                progress_pct=30,
            )
        except Exception as e:
            await _update_job(session, job,
                status=JobStatus.FAILED,
                error_message=f"VIN decode failed: {e}",
                completed_at=datetime.now(timezone.utc),
            )
            return

        # ── Stage 2: Script generation ────────────────────────
        await _update_job(session, job,
            status=JobStatus.SCRIPT_GENERATING,
            progress_pct=40,
        )
        try:
            script = await generate_ad_script(
                vehicle_data=vehicle_data,
                theme=job.theme,
                salesperson_name=user.full_name,
                dealership_name=user.dealership_name,
            )
            await _update_job(session, job,
                generated_script=json.dumps(script),
                progress_pct=50,
            )
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
            await _update_job(session, job,
                status=JobStatus.VOICE_CLONING,
                progress_pct=55,
            )
            try:
                audio_bytes = await text_to_speech(
                    text=script["full_script"],
                    voice_id=user.elevenlabs_voice_id,
                )
                audio_key = make_audio_output_key(job.id)
                upload_bytes(audio_bytes, audio_key, "audio/mpeg")
                audio_s3_key = audio_key
                await _update_job(session, job, progress_pct=65)
            except Exception as e:
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Voice TTS failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Stage 4: Avatar generation ────────────────────────
        avatar_s3_key = None
        if user.heygen_avatar_id and audio_s3_key:
            await _update_job(session, job,
                status=JobStatus.AVATAR_GENERATING,
                progress_pct=70,
            )
            try:
                audio_url = (
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{audio_s3_key}"
                )
                video_id = await submit_video(
                    avatar_id=user.heygen_avatar_id,
                    audio_url=audio_url,
                )
                heygen_video_url = await wait_for_video(video_id)
                video_bytes = await download_video(heygen_video_url)
                avatar_key = make_avatar_output_key(job.id)
                upload_bytes(video_bytes, avatar_key, "video/mp4")
                avatar_s3_key = avatar_key
                await _update_job(session, job,
                    heygen_video_url=heygen_video_url,
                    progress_pct=80,
                )
            except Exception as e:
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Avatar generation failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Stage 5: Video assembly ───────────────────────────
        if avatar_s3_key and audio_s3_key:
            await _update_job(session, job,
                status=JobStatus.ASSEMBLING,
                progress_pct=85,
            )
            try:
                # Public URLs for Shotstack to access
                avatar_url = (
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{avatar_s3_key}"
                )
                audio_url = (
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{audio_s3_key}"
                )

                # Get actual audio duration for precise timing
                audio_duration = get_audio_duration(audio_s3_key)

                # Get car photos from job or use defaults
                car_photos = DEFAULT_CAR_PHOTOS
                if job.car_photo_urls:
                    try:
                        car_photos = json.loads(job.car_photo_urls)
                    except Exception:
                        car_photos = DEFAULT_CAR_PHOTOS

                # Build feature highlights from vehicle data
                vd = json.loads(job.vehicle_data) if job.vehicle_data else {}
                highlights = _build_highlights(vd, user.dealership_name)

                # Build vehicle summary line
                from app.services.vin_decoder import vehicle_summary
                v_summary = vehicle_summary(vd) if vd else "Visit us today"

                # Build and submit the Shotstack timeline
                timeline = build_ad_timeline(
                    avatar_video_url=avatar_url,
                    audio_url=audio_url,
                    car_photo_urls=car_photos,
                    dealership_name=user.dealership_name,
                    vehicle_summary=v_summary,
                    feature_highlights=highlights,
                    duration=audio_duration,
                    hook_pct=0.15,
                    cta_pct=0.15,
                    transition_style="dynamic",
                    brand_color="#C4122F",
                )

                render_id = await submit_render(timeline)
                final_video_url = await wait_for_render(render_id)

                # Download and save final video to S3
                final_bytes = await download_render(final_video_url)
                final_key = make_final_video_key(job.id)
                upload_bytes(final_bytes, final_key, "video/mp4")

                # Generate presigned URL for frontend
                presigned_url = create_presigned_download_url(final_key)

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

        # ── Done ──────────────────────────────────────────────
        await _update_job(session, job,
            status=JobStatus.COMPLETED,
            progress_pct=100,
            completed_at=datetime.now(timezone.utc),
        )


def _build_highlights(vehicle_data: dict, dealership_name: str) -> list[str]:
    """
    Build 3 feature highlight strings from vehicle data.
    These appear as text overlays during the car photo section.
    """
    highlights = []

    if vehicle_data.get("trim"):
        highlights.append(f"{vehicle_data['trim']} Trim")
    if vehicle_data.get("engine"):
        highlights.append(f"{vehicle_data['engine']} Engine")
    if vehicle_data.get("fuel_type"):
        highlights.append(f"{vehicle_data['fuel_type']}")

    # Pad with dealership name if we don't have enough
    while len(highlights) < 3:
        highlights.append(f"Visit {dealership_name}")

    return highlights[:3]


# ── Create job ────────────────────────────────────────────────
@router.post("/", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Create a new ad generation job.
    Returns immediately with status 'pending'.
    Pipeline runs in the background — poll GET /jobs/{id} for progress.
    """
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
        photos_s3_keys=payload.photos_s3_keys,
        voice_s3_key=payload.voice_s3_key,
        car_photo_urls=payload.car_photo_urls,
        status=JobStatus.PENDING,
        progress_pct=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    background_tasks.add_task(
        _run_pipeline,
        job_id=job.id,
        user_id=current_user.id,
    )

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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    if job.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this job.",
        )
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