from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated

import httpx

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
    timeline_max_photos,
    submit_render,
    wait_for_render,
    wait_for_render_with_fallback,
    delete_render,
    download_render,
)
from app.services.s3 import (
    upload_bytes,
    delete_prefix,
    make_audio_output_key,
    make_final_video_key,
    create_presigned_download_url,
    get_audio_duration,
)
from app.services.audio_utils import get_audio_level, calculate_volume_multiplier
from app.services.analytics import track_generation, record_api_usage

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


_PHOTO_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

def _is_access_denied(msg: str) -> bool:
    """Shotstack's error when a dealer CDN blocks its render servers."""
    m = (msg or "").lower()
    return "access denied" in m or "not accessible" in m or "403" in m


async def _rehost_photos_to_s3(photo_urls: list, job_id: int) -> list:
    """
    Download each photo server-side and re-upload to S3, returning public S3 URLs
    (bucket policy grants public read to outputs/*). Used ONLY when Shotstack is
    blocked from the dealer's image CDN. Best-effort per photo (keeps the original
    URL on failure). Objects live under outputs/{job_id}/photos/ and are deleted
    after the render completes.
    """
    from app.core.config import get_settings
    settings = get_settings()
    out = []
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers=_PHOTO_FETCH_HEADERS
    ) as client:
        for i, url in enumerate(photo_urls):
            if not url or not str(url).startswith("http"):
                out.append(url)
                continue
            try:
                resp = await client.get(url)
                if resp.status_code != 200 or not resp.content:
                    out.append(url)
                    continue
                # Downsample to ~1280px JPEG (render is 1080p) — big S3/bandwidth
                # savings vs. the 2-3 MB dealer originals. Off-loop (Pillow is CPU).
                from app.services.image_utils import downsample_image_bytes
                body = await asyncio.to_thread(downsample_image_bytes, resp.content, 1280, 85)
                key = f"outputs/{job_id}/photos/{i}.jpg"
                await asyncio.to_thread(upload_bytes, body, key, "image/jpeg")
                out.append(
                    f"https://{settings.s3_bucket_name}.s3."
                    f"{settings.aws_region}.amazonaws.com/{key}"
                )
            except Exception as e:
                print(f"Photo re-host failed for {url}: {e}")
                out.append(url)
    return out


# ── Pipeline ──────────────────────────────────────────────────
async def _run_pipeline(job_id: int, user_id: int):
    """
    Ad generation pipeline — runs in the background.

    Stages:
      1. VIN decode         (10% → 30%)
      2. Script generation  (30% → 50%)
      3. Voice TTS          (50% → 70%)
      4. Video assembly     (70% → 100%)

    Each stage has its own try/except that marks the job FAILED and returns.
    The outer try/except below is a safety net that catches anything that
    slips through — e.g. exceptions in the initial DB fetch, between-stage
    code, or the final COMPLETED write — so the job is never silently stuck.
    """
    try:
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
                    user_id=user_id,
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
                user_id=user_id,
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
                # 10s fallback (matches the recommended max) — used only for old
                # outros uploaded before duration_seconds was captured on upload.
                outro_duration   = 10.0
                slideshow_volume = 1.0
                if job_video_type == "with_outro" and job_outro_video_id:
                    async with AsyncSessionLocal() as session:
                        outro = await session.get(OutroVideo, job_outro_video_id)
                        if outro and outro.user_id == user_id:
                            outro_s3_key   = outro.s3_key
                            outro_url      = create_presigned_download_url(outro_s3_key, expires_in=3600)
                            outro_duration = outro.duration_seconds or 10.0
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

                # Only the photos that will actually appear in the render (not the
                # whole scraped set) — so any re-hosting downloads the minimum.
                used_photos = car_photos[:timeline_max_photos(audio_duration)]

                def _make_timeline(photos):
                    return build_ad_timeline_photo_only(
                        audio_url=audio_url,
                        car_photo_urls=photos,
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

                async def _submit_and_wait(photos):
                    rid = await submit_render(_make_timeline(photos))
                    await _update_job_safe(job_id,
                        shotstack_render_id=rid,
                        status=JobStatus.ASSEMBLING,
                        progress_pct=85,
                    )
                    print(f"Shotstack render submitted: {rid} — waiting for webhook or fallback poll")
                    return rid, await wait_for_render_with_fallback(rid)

                # Proxy proactively only for hosts already known (persisted) to block
                # Shotstack; otherwise send the dealer URLs straight to Shotstack (no
                # download). Unknown blockers are caught by the retry path below.
                from app.services import photo_proxy
                await photo_proxy.ensure_loaded()
                proxied = photo_proxy.any_blocked(used_photos)
                if proxied:
                    print("Photo host known to block Shotstack — re-hosting used photos to S3")
                    used_photos = await _rehost_photos_to_s3(used_photos, job_id)

                try:
                    render_id, fallback_url = await _submit_and_wait(used_photos)
                except RuntimeError as render_err:
                    # Dynamic recovery: if Shotstack was blocked from the image CDN,
                    # re-host the used photos to S3 and retry once — instead of failing
                    # the job. Works for ANY blocking host, and PERSISTS it so future
                    # jobs proxy proactively (no wasted first render next time).
                    if not proxied and _is_access_denied(str(render_err)):
                        await photo_proxy.add_blocked_hosts(
                            [photo_proxy.host_of(u) for u in used_photos], source="runtime"
                        )
                        print(f"Shotstack blocked from photo host ({render_err}) — re-hosting to S3 and retrying")
                        used_photos = await _rehost_photos_to_s3(used_photos, job_id)
                        proxied = True
                        render_id, fallback_url = await _submit_and_wait(used_photos)
                    else:
                        raise

                if fallback_url:
                    # Webhook didn't fire (dev, or missed) — fallback polling got the URL
                    final_bytes = await download_render(fallback_url)
                    final_key = make_final_video_key(job_id)
                    # Run the synchronous boto3 upload in a thread so the multi-MB
                    # PUT doesn't block the event loop (bug #49). On the single-worker
                    # dev server that freeze stalled every concurrent request — incl.
                    # the extension's /auth/me and job polls — showing a spurious
                    # logout + stuck-at-assembling until the upload finished.
                    await asyncio.to_thread(upload_bytes, final_bytes, final_key, "video/mp4")
                    presigned_url = create_presigned_download_url(final_key, expires_in=604800)

                    # Log the render cost (fallback-completion path — the dev path,
                    # and any prod job whose webhook didn't deliver the URL). quantity
                    # is the same render-duration value that feeds AdEvent.render_time_seconds
                    # (now - job_created_at), so the two numbers agree. The webhook
                    # path logs the same call_type at its own upload site (bug #49
                    # touched both; so does this).
                    try:
                        render_secs = int((datetime.utcnow() - job_created_at.replace(tzinfo=None)).total_seconds())
                        await record_api_usage(
                            "video_render",
                            user_id=user_id,
                            quantity=render_secs,
                            model=None,
                        )
                    except Exception as usage_err:
                        print(f"[usage] video_render (fallback) log failed: {usage_err}")

                    try:
                        await delete_render(render_id)
                    except Exception:
                        pass

                    # Clean up any re-hosted photo proxies for this job (no-op if none).
                    await asyncio.to_thread(delete_prefix, f"outputs/{job_id}/photos/")

                    await _update_job_safe(job_id,
                        final_video_s3_key=final_key,
                        final_video_url=presigned_url,
                        progress_pct=100,
                    )

            except Exception as e:
                # Clean up re-hosted photo proxies even on failure (no-op if none).
                try:
                    await asyncio.to_thread(delete_prefix, f"outputs/{job_id}/photos/")
                except Exception:
                    pass
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

    except Exception as exc:
        # Safety net: catches anything that slipped through the per-stage handlers
        # (e.g. an exception in the initial DB fetch, between-stage code, or the
        # final COMPLETED write). Per-stage handlers already return after marking
        # FAILED, so they never reach here. This only fires for truly unhandled paths.
        msg = f"Unexpected pipeline error: {exc}"
        print(f"[pipeline] outer catch — job_id={job_id}: {msg}")
        try:
            await _update_job_safe(
                job_id,
                status=JobStatus.FAILED,
                error_message=msg,
                completed_at=datetime.utcnow(),
            )
        except Exception as mark_err:
            print(f"[pipeline] also failed to mark job FAILED: {mark_err}")


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
            # Upload off the event loop (bug #49) so the PUT doesn't block other requests.
            await asyncio.to_thread(upload_bytes, final_bytes, final_key, "video/mp4")
            presigned_url = create_presigned_download_url(final_key, expires_in=604800)

            # Log the render cost (webhook-completion path — the prod path).
            # Same render-duration value (now - job.created_at) that feeds
            # AdEvent.render_time_seconds, so the two agree. Mirrors the
            # fallback-path logging above — both completion paths log video_render.
            try:
                # .replace(tzinfo=None): job.created_at may be aware in prod
                # (some tables are `timestamp WITH time zone`), naive in dev.
                render_secs = int((datetime.utcnow() - job.created_at.replace(tzinfo=None)).total_seconds())
                await record_api_usage(
                    "video_render",
                    user_id=job.user_id,
                    quantity=render_secs,
                    model=None,
                )
            except Exception as usage_err:
                print(f"[usage] video_render (webhook) log failed: {usage_err}")

            try:
                await delete_render(render_id)
                print(f"Deleted render {render_id} from Shotstack")
            except Exception:
                pass

            # Clean up any re-hosted photo proxies for this job (no-op if none).
            await asyncio.to_thread(delete_prefix, f"outputs/{job.id}/photos/")

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

    # Effective entitlement (dealership inheritance) — a dealership salesperson
    # reads active/'dealership' even though their own row is trial/none, so the
    # gates below don't misfire on them. (require_active_subscription already
    # blocked dealership_inactive + own-expired before we got here.)
    from app.services.entitlement import resolve_entitlement
    _ent = await resolve_entitlement(current_user, session)
    eff_status, eff_plan = _ent["status"], _ent["plan"]

    # Trial-completion gate (authoritative — mirrors the pipeline so a bypassed
    # client can't create jobs after the trial is finished). The 5-video cap is
    # enforced only here + in the pipeline. Only applies to OWN trial accounts.
    if eff_status == "trial":
        from app.core.config import get_settings
        settings = get_settings()
        is_dev = bool(settings.dev_test_email and current_user.email == settings.dev_test_email)
        if not is_dev:
            now = datetime.now(timezone.utc)
            trial_end = current_user.trial_ends_at or now
            if trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)
            if now > trial_end:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="TRIAL_EXPIRED")
            if (current_user.trial_video_count or 0) >= 5:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="TRIAL_VIDEO_LIMIT")

    # Server-side entitlement gate (authoritative — the extension's outro gate is
    # UX only and can be bypassed by editing the client). Personal outros are an
    # Elite/Dealership feature; trial users get them during the trial. Uses the
    # EFFECTIVE plan so dealership team members (plan='dealership') are allowed.
    if payload.video_type == "with_outro":
        is_trial   = eff_status == "trial"
        has_outro  = eff_plan in ("elite", "dealership")
        if not is_trial and not has_outro:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OUTRO_REQUIRES_ELITE",
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
