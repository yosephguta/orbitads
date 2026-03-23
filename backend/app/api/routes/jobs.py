from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.job import Job, JobCreate, JobRead, JobStatus
from app.models.user import User
from app.services.vin_decoder import decode_vin
from app.services.script_generator import generate_ad_script

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ── Helper ────────────────────────────────────────────────────
async def _update_job(session: AsyncSession, job: Job, **kwargs) -> None:
    """
    Update job fields and always set updated_at to now.
    Commits immediately so the frontend sees live status updates.
    """
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc)
    session.add(job)
    await session.commit()
    await session.refresh(job)


# ── Create job + run pipeline ─────────────────────────────────
@router.post("/", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Create a new ad generation job and run the Phase 1 pipeline:
      1. Decode VIN via NHTSA API
      2. Generate ad script via Claude API

    Returns the completed job with vehicle_data and generated_script populated.

    Phase 2 will move the heavy AI steps (voice, avatar) to a background
    queue — but for Phase 1 we run everything synchronously since it's fast.
    """
    # ── Validate input ────────────────────────────────────────
    if not payload.vin and not payload.listing_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please provide either a VIN or a listing URL.",
        )

    # ── Create the job record ─────────────────────────────────
    # We save it immediately so there's a record even if the pipeline fails
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

    # ── Stage 1: VIN decode ───────────────────────────────────
    await _update_job(session, job,
        status=JobStatus.VIN_DECODING,
        progress_pct=10,
    )

    try:
        vehicle_data = await decode_vin(payload.vin) if payload.vin else {}
        await _update_job(session, job,
            vehicle_data=json.dumps(vehicle_data),
            progress_pct=40,
        )
    except Exception as e:
        await _update_job(session, job,
            status=JobStatus.FAILED,
            error_message=f"VIN decode failed: {e}",
            completed_at=datetime.now(timezone.utc),
        )
        return job

    # ── Stage 2: Script generation ────────────────────────────
    await _update_job(session, job,
        status=JobStatus.SCRIPT_GENERATING,
        progress_pct=50,
    )

    try:
        script = await generate_ad_script(
            vehicle_data=vehicle_data,
            theme=payload.theme,
            salesperson_name=current_user.full_name,
            dealership_name=current_user.dealership_name,
        )
        await _update_job(session, job,
            generated_script=json.dumps(script),
            status=JobStatus.COMPLETED,
            progress_pct=100,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        await _update_job(session, job,
            status=JobStatus.FAILED,
            error_message=f"Script generation failed: {e}",
            completed_at=datetime.now(timezone.utc),
        )

    return job


# ── Get single job ────────────────────────────────────────────
@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Get a job by ID. Used by the frontend to poll for status updates.
    Returns 404 if not found, 403 if it belongs to a different user.
    """
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


# ── List all jobs for current user ────────────────────────────
@router.get("/", response_model=list[JobRead])
async def list_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 20,
    offset: int = 0,
):
    """
    Return the current user's job history, newest first.
    Used by the frontend ad library / dashboard.
    """
    result = await session.exec(
        select(Job)
        .where(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()