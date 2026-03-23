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
from app.services.s3 import (
    upload_bytes,
    make_audio_output_key,
    make_avatar_output_key,
    create_presigned_download_url,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ── Helper ────────────────────────────────────────────────────
async def _update_job(session: AsyncSession, job: Job, **kwargs) -> None:
    """Update job fields, always set updated_at, and commit immediately."""
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    await session.commit()
    await session.refresh(job)


# ── Background pipeline ───────────────────────────────────────
async def _run_pipeline(job_id: int, user_id: int, db_url: str):
    """
    Full ad generation pipeline — runs in the background after
    the HTTP response is sent so the frontend gets the job ID immediately.

    Stages:
      1. VIN decode
      2. Script generation (Claude)
      3. Voice TTS (ElevenLabs) — only if user has a voice_id
      4. Avatar video (HeyGen)  — only if user has an avatar_id
    """
    # We need a fresh database session since this runs outside the request
    from app.core.database import AsyncSessionLocal

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
                progress_pct=55,
            )
        except Exception as e:
            await _update_job(session, job,
                status=JobStatus.FAILED,
                error_message=f"Script generation failed: {e}",
                completed_at=datetime.now(timezone.utc),
            )
            return

        # ── Stage 3: Voice TTS ────────────────────────────────
        # Only runs if the user has set up their ElevenLabs voice
        audio_s3_key = None
        if user.elevenlabs_voice_id:
            await _update_job(session, job,
                status=JobStatus.VOICE_CLONING,
                progress_pct=65,
            )
            try:
                full_script = script["full_script"]
                audio_bytes = await text_to_speech(
                    text=full_script,
                    voice_id=user.elevenlabs_voice_id,
                )
                # Save audio to S3
                audio_key = make_audio_output_key(job.id)
                upload_bytes(audio_bytes, audio_key, "audio/mpeg")
                audio_s3_key = audio_key
                await _update_job(session, job,
                    progress_pct=75,
                )
            except Exception as e:
                # TTS failure is non-fatal — we can still proceed
                # without voice if needed, or fail the job
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Voice TTS failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Stage 4: Avatar video ─────────────────────────────
        # Only runs if user has avatar_id AND we have audio
        if user.heygen_avatar_id and audio_s3_key:
            await _update_job(session, job,
                status=JobStatus.AVATAR_GENERATING,
                progress_pct=80,
            )
            try:
                from app.core.config import get_settings
                settings = get_settings()

                # Build public audio URL for HeyGen
                audio_url = (
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{audio_s3_key}"
                )

                # Submit to HeyGen and wait for completion
                video_id = await submit_video(
                    avatar_id=user.heygen_avatar_id,
                    audio_url=audio_url,
                )
                heygen_video_url = await wait_for_video(video_id)

                # Download and save to our S3
                video_bytes = await download_video(heygen_video_url)
                avatar_key = make_avatar_output_key(job.id)
                upload_bytes(video_bytes, avatar_key, "video/mp4")

                # Generate a presigned download URL for the frontend
                final_url = create_presigned_download_url(avatar_key)

                await _update_job(session, job,
                    heygen_video_url=heygen_video_url,
                    final_video_s3_key=avatar_key,
                    final_video_url=final_url,
                    progress_pct=100,
                )
            except Exception as e:
                await _update_job(session, job,
                    status=JobStatus.FAILED,
                    error_message=f"Avatar generation failed: {e}",
                    completed_at=datetime.now(timezone.utc),
                )
                return

        # ── Done ──────────────────────────────────────────────
        await _update_job(session, job,
            status=JobStatus.COMPLETED,
            progress_pct=100,
            completed_at=datetime.now(timezone.utc),
        )


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

    Returns the job immediately with status "pending".
    The pipeline runs in the background — poll GET /jobs/{id}
    every 5 seconds to check progress.

    Progress stages:
      0%   → pending
      10%  → vin_decoding
      40%  → script_generating
      65%  → voice_cloning
      80%  → avatar_generating
      100% → completed
    """
    if not payload.vin and not payload.listing_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please provide either a VIN or a listing URL.",
        )

    # Create the job record immediately
    job = Job(
        user_id=current_user.id,
        vin=payload.vin,
        listing_url=payload.listing_url,
        theme=payload.theme,
        photos_s3_keys=payload.photos_s3_keys,
        voice_s3_key=payload.voice_s3_key,
        status=JobStatus.PENDING,
        progress_pct=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Kick off the pipeline in the background
    # The HTTP response returns here — client gets job_id immediately
    from app.core.config import get_settings
    settings = get_settings()

    background_tasks.add_task(
        _run_pipeline,
        job_id=job.id,
        user_id=current_user.id,
        db_url=settings.database_url,
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