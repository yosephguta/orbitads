"""
Admin-only routes for dealership + salesperson account management.

Everything here is mounted under /admin and gated by get_current_admin
(app/core/security.py, added in Part 1) — role must be 'admin'.

Scope (Part 2):
  - Dealership CRUD
  - Assign/change a dealership's manager (writes both sides in one txn)
  - Create a salesperson account directly (bypasses signup/verification)
  - Bulk-assign existing salespeople to a dealership by email list
  - Manual plan/subscription grant for any existing user
  - List/search users

NOTE (Python 3.9): use Optional[...]/List[...] from typing in every
annotation FastAPI/Pydantic evaluates — never `X | None` (see CLAUDE.md #46).
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core.database import get_session
from app.core.security import get_current_admin, hash_password
from app.models.dealership import Dealership
from app.models.user import User, UserRead
from app.models.dealer_platform import DealerPlatform
from app.models.ad_event import AdEvent
from app.models.api_usage import ApiUsage
from app.services.analytics import PRELOADED_VOICE_IDS
from app.services.send_weekly_reports import (
    send_dealership_weekly_report,
    last_week_window,
)

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_PLANS = {"pro", "elite", "dealership"}
VALID_STATUS = {"active", "trial", "past_due", "cancelled"}

# Defaults for admin-created salespeople — mirror register() so the accounts
# are fully usable (voice features expect these set).
DEFAULT_VOICE_EN = "Gubgw9l4dtIoQA9YZHgx"   # Brian
DEFAULT_VOICE_ES = "zDMHo7CPscBTgfDtPOWl"   # Claus


# ── Request bodies ────────────────────────────────────────────
class DealershipCreate(BaseModel):
    dealer_group: str
    dealership_name: str
    location: Optional[str] = None
    required_tagline: Optional[str] = None
    required_tagline_es: Optional[str] = None
    website_url: Optional[str] = None


class DealershipUpdate(BaseModel):
    dealer_group: Optional[str] = None
    dealership_name: Optional[str] = None
    location: Optional[str] = None
    required_tagline: Optional[str] = None
    required_tagline_es: Optional[str] = None
    website_url: Optional[str] = None


class AssignManagerBody(BaseModel):
    user_id: int


class AdminUserCreate(BaseModel):
    email: str
    full_name: str
    first_name: str
    last_name: str
    password: str
    dealership_id: Optional[int] = None
    purchased_plan: Optional[str] = None
    subscription_status: Optional[str] = None


class BulkAssignBody(BaseModel):
    emails: List[str]
    purchased_plan: Optional[str] = None
    subscription_status: Optional[str] = None


class PlanGrantBody(BaseModel):
    purchased_plan: Optional[str] = None
    subscription_status: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────
def _validate_plan_status(plan: Optional[str], status_val: Optional[str]) -> None:
    """Reject unknown plan/status values so a typo can't write garbage."""
    if plan is not None and plan not in VALID_PLANS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"purchased_plan must be one of {sorted(VALID_PLANS)}.",
        )
    if status_val is not None and status_val not in VALID_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subscription_status must be one of {sorted(VALID_STATUS)}.",
        )


async def _enrich_dealership(
    session: SQLModelAsyncSession, dealership: Dealership
) -> dict:
    """
    Serialize a dealership plus its current manager (User with role='manager'
    AND dealership_id == this dealership) and a salesperson count. The admin UI
    needs both to pick a dealership to act on.

    Manager is looked up by (role, dealership_id) rather than trusting
    dealership.manager_user_id — dealership_id-on-User is the source of truth
    (Part 1). They should always agree; querying the User side surfaces drift
    rather than hiding it.
    """
    manager_result = await session.exec(
        select(User).where(
            User.dealership_id == dealership.id,
            User.role == "manager",
        )
    )
    manager = manager_result.first()

    salesperson_count = (
        await session.exec(
            select(func.count())
            .select_from(User)
            .where(
                User.dealership_id == dealership.id,
                User.role == "salesperson",
            )
        )
    ).one()

    return {
        "id": dealership.id,
        "dealer_group": dealership.dealer_group,
        "dealership_name": dealership.dealership_name,
        "location": dealership.location,
        "manager_user_id": dealership.manager_user_id,
        "invite_code": dealership.invite_code,
        "required_tagline": dealership.required_tagline,
        "required_tagline_es": dealership.required_tagline_es,
        "platform_id": dealership.platform_id,
        "website_url": dealership.website_url,
        "created_at": dealership.created_at,
        "manager": (
            {
                "id": manager.id,
                "email": manager.email,
                "full_name": manager.full_name,
            }
            if manager
            else None
        ),
        "salesperson_count": salesperson_count,
    }


# ── STEP 1: Dealership CRUD ───────────────────────────────────
@router.post("/dealerships", status_code=status.HTTP_201_CREATED)
async def create_dealership(
    payload: DealershipCreate,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    dealership = Dealership(
        dealer_group=payload.dealer_group,
        dealership_name=payload.dealership_name,
        location=payload.location,
        required_tagline=payload.required_tagline,
        required_tagline_es=payload.required_tagline_es,
        website_url=payload.website_url,
        # invite_code and platform_id intentionally unset — not part of this flow.
    )
    session.add(dealership)
    await session.commit()
    await session.refresh(dealership)
    return await _enrich_dealership(session, dealership)


@router.get("/dealerships")
async def list_dealerships(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    result = await session.exec(select(Dealership).order_by(Dealership.created_at.desc()))
    dealerships = result.all()
    return [await _enrich_dealership(session, d) for d in dealerships]


@router.get("/dealerships/{dealership_id}")
async def get_dealership(
    dealership_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")
    return await _enrich_dealership(session, dealership)


@router.patch("/dealerships/{dealership_id}")
async def update_dealership(
    dealership_id: int,
    payload: DealershipUpdate,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")

    # Only overwrite fields the caller actually sent (exclude_unset) so a
    # partial PATCH doesn't null out untouched columns.
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(dealership, field, value)

    session.add(dealership)
    await session.commit()
    await session.refresh(dealership)
    return await _enrich_dealership(session, dealership)


# ── STEP 2: Assign/change a dealership's manager ──────────────
@router.post("/dealerships/{dealership_id}/assign-manager")
async def assign_manager(
    dealership_id: int,
    payload: AssignManagerBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Make `user_id` the manager of this dealership, writing BOTH sides
    (user.role/user.dealership_id AND dealership.manager_user_id) in one
    transaction — a partial write here would break the
    dealership_id-is-source-of-truth invariant from Part 1.

    Also demotes the outgoing manager (if any, and different) back to
    'salesperson', and clears a stale manager pointer on any OTHER dealership
    the new manager currently manages — so we never leave a manager-role user
    whose dealership_id points somewhere its manager_user_id doesn't agree.
    """
    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")

    target = await session.get(User, payload.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Demote the outgoing manager of THIS dealership (if different).
    old_manager_id = dealership.manager_user_id
    if old_manager_id and old_manager_id != target.id:
        old_manager = await session.get(User, old_manager_id)
        if old_manager and old_manager.role == "manager":
            old_manager.role = "salesperson"
            old_manager.updated_at = datetime.utcnow()
            session.add(old_manager)

    # If the new manager was managing a DIFFERENT dealership, clear that
    # dealership's manager pointer (their dealership_id is about to move here).
    if (
        target.role == "manager"
        and target.dealership_id is not None
        and target.dealership_id != dealership_id
    ):
        other = await session.get(Dealership, target.dealership_id)
        if other and other.manager_user_id == target.id:
            other.manager_user_id = None
            session.add(other)

    # Promote + attach the target.
    target.role = "manager"
    target.dealership_id = dealership_id
    target.updated_at = datetime.utcnow()
    session.add(target)

    dealership.manager_user_id = target.id
    session.add(dealership)

    # Single commit → the whole reassignment is one atomic transaction.
    await session.commit()
    await session.refresh(dealership)
    return await _enrich_dealership(session, dealership)


# ── STEP 3: Create a salesperson account directly ─────────────
@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Create a salesperson account directly (no email verification flow).
    role is always 'salesperson' here — promoting to manager is a separate
    action (assign-manager, Step 2).
    """
    _validate_plan_status(payload.purchased_plan, payload.subscription_status)

    email = payload.email.lower().strip()
    existing = await session.exec(select(User).where(User.email == email))
    if existing.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # status: explicit value wins; else 'active' if a plan was set, else 'trial'.
    sub_status = payload.subscription_status
    if sub_status is None:
        sub_status = "active" if payload.purchased_plan else "trial"

    user = User(
        email=email,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role="salesperson",
        is_verified=True,      # admin-created — no unverified state to represent
        dealership_id=payload.dealership_id,
        purchased_plan=payload.purchased_plan,
        subscription_status=sub_status,
        elevenlabs_voice_id=DEFAULT_VOICE_EN,
        elevenlabs_voice_id_es=DEFAULT_VOICE_ES,
        # Only trial accounts get a trial window; paid ones don't need one.
        trial_ends_at=(datetime.utcnow() + timedelta(days=7)) if sub_status == "trial" else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return UserRead.model_validate(user).model_dump()


# ── STEP 4: Bulk-assign existing salespeople to a dealership ──
@router.post("/dealerships/{dealership_id}/bulk-assign")
async def bulk_assign(
    dealership_id: int,
    payload: BulkAssignBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Assign a pasted list of already-registered salespeople to this dealership
    and graduate them off trial (set purchased_plan + subscription_status).
    Never touches role — this is for the team, never for assigning a manager.

    Missing emails are collected and reported, not errored, so one typo
    doesn't fail the assignment of everyone else.
    """
    _validate_plan_status(payload.purchased_plan, payload.subscription_status)

    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")

    results = []
    for raw_email in payload.emails:
        email = raw_email.lower().strip()
        found = await session.exec(select(User).where(User.email == email))
        user = found.first()
        if not user:
            results.append({"email": raw_email, "status": "not_found"})
            continue

        user.dealership_id = dealership_id
        if payload.purchased_plan is not None:
            user.purchased_plan = payload.purchased_plan
        if payload.subscription_status is not None:
            user.subscription_status = payload.subscription_status
        user.updated_at = datetime.utcnow()
        session.add(user)
        results.append({"email": raw_email, "status": "assigned", "user_id": user.id})

    await session.commit()
    assigned = sum(1 for r in results if r["status"] == "assigned")
    return {
        "dealership_id": dealership_id,
        "assigned": assigned,
        "not_found": len(results) - assigned,
        "results": results,
    }


# ── STEP 5: Manual plan/subscription grant for any user ───────
@router.patch("/users/{user_id}/plan")
async def grant_plan(
    user_id: int,
    payload: PlanGrantBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Set purchased_plan / subscription_status on an existing user. Standalone
    version of what bulk-assign does inline — for a user already on a
    dealership, or an individual account closed directly.

    (scripts/reconcile_plans.py has a 'grant' mode, but it's raw asyncpg,
    email-scoped, with DRY_RUN prints — it does NOT factor into an
    AsyncSession-based route helper, so this is written fresh. See report.)
    """
    _validate_plan_status(payload.purchased_plan, payload.subscription_status)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if payload.purchased_plan is not None:
        user.purchased_plan = payload.purchased_plan
    if payload.subscription_status is not None:
        user.subscription_status = payload.subscription_status
    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return UserRead.model_validate(user).model_dump()


# ── STEP 6: List/search users ─────────────────────────────────
@router.get("/users")
async def list_users(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    dealership_id: Optional[int] = Query(default=None),
    role: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = []
    if dealership_id is not None:
        filters.append(User.dealership_id == dealership_id)
    if role is not None:
        filters.append(User.role == role)
    if email is not None:
        filters.append(User.email.ilike(f"%{email.lower().strip()}%"))

    total = (
        await session.exec(select(func.count()).select_from(User).where(*filters))
    ).one()

    result = await session.exec(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    users = result.all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "users": [UserRead.model_validate(u).model_dump() for u in users],
    }


# ══════════════════════════════════════════════════════════════
# PART 3
# ══════════════════════════════════════════════════════════════

# ── STEP 1: On-demand weekly report trigger ───────────────────
@router.post("/dealerships/{dealership_id}/send-weekly-report")
async def trigger_weekly_report(
    dealership_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Trigger the manager (team) weekly report for a single dealership, on demand.
    Reuses send_dealership_weekly_report() — the exact same function the Monday
    cron loop calls — so there's no forked report logic. Returns the report's
    own summary (recipient, date range, counts, sent flag / skip reason).
    """
    dealership = await session.get(Dealership, dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")

    week_start, week_end = last_week_window()
    summary = await send_dealership_weekly_report(
        session, dealership, week_start, week_end
    )
    return summary


# ── STEP 2: DealerPlatform review queue ───────────────────────
class RejectBody(BaseModel):
    reason: Optional[str] = None


@router.get("/dealer-platforms")
async def list_dealer_platforms(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    # aliased to `status` in the URL; the Python name avoids shadowing the
    # imported `status` module.
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="pending_review | active | rejected"
    ),
):
    """
    Review-queue list. `status` filter serves both the review queue
    (?status=pending_review) and the 'what's already live' view (?status=active).
    Omit status to return everything.
    """
    valid = {"pending_review", "active", "rejected"}
    if status_filter is not None and status_filter not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(valid)}.",
        )

    query = select(DealerPlatform)
    if status_filter is not None:
        query = query.where(DealerPlatform.status == status_filter)
    query = query.order_by(DealerPlatform.created_at.desc())

    result = await session.exec(query)
    platforms = result.all()
    return {
        "count": len(platforms),
        "status_filter": status_filter,
        "platforms": [
            {
                "id": p.id,
                "name": p.name,
                "platform_slug": p.platform_slug,
                "status": p.status,
                "source_url": p.source_url,
                "notes": p.notes,
                "warnings": p.generation_warnings,
                "input_tokens": p.input_tokens,
                "output_tokens": p.output_tokens,
                "reviewed_at": p.reviewed_at,
                "reviewed_by": p.reviewed_by,
                "created_at": p.created_at,
                "config_preview": {
                    "platform": p.config_json.get("platform"),
                    "vehicle_cards": p.config_json.get("inventory", {}).get("vehicle_cards"),
                    "sale_price": p.config_json.get("detail_page", {}).get("sale_price"),
                },
            }
            for p in platforms
        ],
    }


@router.post("/dealer-platforms/{platform_id}/approve")
async def approve_dealer_platform(
    platform_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Approve a pending config → status=active. Same effect as the old
    /dealer-configs/{id}/approve, now admin-gated. Note: this only flips the
    platform's status + review metadata — the dealership↔platform link
    (dealership.platform_id) is set at config-generation time, not here, so no
    dealership rows are touched (mirrors the original approve logic).
    """
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    platform.status = "active"
    platform.reviewed_at = datetime.utcnow()
    platform.reviewed_by = _admin.email
    session.add(platform)
    await session.commit()

    return {
        "message": f"Config {platform_id} approved and now active",
        "platform_id": platform_id,
        "platform_slug": platform.platform_slug,
        "source_url": platform.source_url,
        "status": platform.status,
    }


@router.post("/dealer-platforms/{platform_id}/reject")
async def reject_dealer_platform(
    platform_id: int,
    payload: RejectBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Reject a config → status=rejected. DealerPlatform has NO dedicated `reason`
    column (only `notes`), so per the brief we don't invent one — the reason is
    appended to the existing `notes` field (same pattern as flag-manual) so it's
    not lost. A dedicated `rejection_reason` column would need a migration.
    """
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    platform.status = "rejected"
    platform.reviewed_at = datetime.utcnow()
    platform.reviewed_by = _admin.email
    if payload.reason:
        stamp = datetime.utcnow().strftime("%Y-%m-%d")
        platform.notes = (platform.notes or "") + (
            f"\n[REJECTED {stamp} by {_admin.email}] {payload.reason}"
        )
    session.add(platform)
    await session.commit()

    return {
        "message": f"Config {platform_id} rejected",
        "platform_id": platform_id,
        "status": platform.status,
        "reason_stored_in_notes": bool(payload.reason),
    }


# ── STEP 3: Live analytics endpoints ──────────────────────────
def _parse_since(since: Optional[str]) -> datetime:
    """
    Parse the `since` ISO date/datetime param → aware-UTC datetime. Default =
    now - 7 days. Aware UTC matches the weekly-report filtering pattern and is
    correct against prod's timestamptz columns (CLAUDE.md #54).
    """
    if not since:
        return datetime.now(timezone.utc) - timedelta(days=7)
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`since` must be ISO format, e.g. 2026-08-01 or 2026-08-01T00:00:00.",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/analytics/overview")
async def analytics_overview(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    since: Optional[str] = Query(default=None, description="ISO date; default last 7 days"),
    dealership_id: Optional[int] = Query(default=None),
):
    since_dt = _parse_since(since)

    # AdEvent carries dealership_id directly, so the scope filter is a simple
    # column match (no join needed here).
    base = [AdEvent.created_at >= since_dt]
    if dealership_id is not None:
        base.append(AdEvent.dealership_id == dealership_id)

    # active_users — distinct user_id in the window
    active_users = (
        await session.exec(
            select(func.count(func.distinct(AdEvent.user_id))).where(*base)
        )
    ).one()

    # format_split — generated events grouped by video_format
    fmt_rows = (
        await session.exec(
            select(AdEvent.video_format, func.count())
            .where(*base, AdEvent.event_type == "generated")
            .group_by(AdEvent.video_format)
        )
    ).all()
    format_split = {(fmt or "unknown"): cnt for fmt, cnt in fmt_rows}

    # top_voices — voice_id_used counts for generated, top 10, labeled
    voice_rows = (
        await session.exec(
            select(AdEvent.voice_id_used, func.count())
            .where(
                *base,
                AdEvent.event_type == "generated",
                AdEvent.voice_id_used.is_not(None),
            )
            .group_by(AdEvent.voice_id_used)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    top_voices = [
        {
            "voice_id": vid,
            "count": cnt,
            "type": "preloaded" if vid in PRELOADED_VOICE_IDS else "custom",
        }
        for vid, cnt in voice_rows
    ]

    # success_rate — generated vs generation_failed, both counts + ratio
    generated_count = (
        await session.exec(
            select(func.count()).select_from(AdEvent)
            .where(*base, AdEvent.event_type == "generated")
        )
    ).one()
    failed_count = (
        await session.exec(
            select(func.count()).select_from(AdEvent)
            .where(*base, AdEvent.event_type == "generation_failed")
        )
    ).one()
    denom = generated_count + failed_count
    success_ratio = (generated_count / denom) if denom else None

    return {
        "since": since_dt.isoformat(),
        "dealership_id": dealership_id,
        "active_users": active_users,
        "format_split": format_split,
        "top_voices": top_voices,
        "success_rate": {
            "generated": generated_count,
            "failed": failed_count,
            "total": denom,
            "ratio": round(success_ratio, 4) if success_ratio is not None else None,
        },
    }


@router.get("/analytics/costs")
async def analytics_costs(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    since: Optional[str] = Query(default=None, description="ISO date; default last 7 days"),
    dealership_id: Optional[int] = Query(default=None),
):
    """
    Raw ApiUsage aggregation. Returns quantity + token SUMS per call_type and in
    total. NO dollar amounts: there are no cost-per-unit constants anywhere in
    the codebase (usage_report.py defers dollar totals to the Anthropic console),
    and model pricing drifts — so pricing is attached on the frontend in one
    place, not hardcoded into this route. (Path taken: raw sums. See report.)

    Rows with user_id IS NULL can't be tied to a dealership; they're always
    surfaced in a separate `unattributed` bucket rather than dropped — including
    when a dealership_id filter is applied (the join would otherwise silently
    exclude them).
    """
    since_dt = _parse_since(since)

    def sums():
        return (
            func.coalesce(func.sum(ApiUsage.quantity), 0),
            func.coalesce(func.sum(ApiUsage.input_tokens), 0),
            func.coalesce(func.sum(ApiUsage.output_tokens), 0),
            func.count(),
        )

    q, it, ot, rows = sums()

    if dealership_id is not None:
        # Scope attributed rows by joining user_id -> User.dealership_id.
        scoped_where = [
            ApiUsage.created_at >= since_dt,
            ApiUsage.user_id == User.id,
            User.dealership_id == dealership_id,
        ]
        by_type_rows = (
            await session.exec(
                select(ApiUsage.call_type, q, it, ot, rows)
                .where(*scoped_where)
                .group_by(ApiUsage.call_type)
                .order_by(func.sum(ApiUsage.quantity).desc())
            )
        ).all()
        total_row = (
            await session.exec(select(q, it, ot, rows).where(*scoped_where))
        ).one()
    else:
        base_where = [ApiUsage.created_at >= since_dt]
        by_type_rows = (
            await session.exec(
                select(ApiUsage.call_type, q, it, ot, rows)
                .where(*base_where)
                .group_by(ApiUsage.call_type)
                .order_by(func.sum(ApiUsage.quantity).desc())
            )
        ).all()
        total_row = (
            await session.exec(select(q, it, ot, rows).where(*base_where))
        ).one()

    # Unattributed bucket — user_id IS NULL, always shown separately, never
    # scoped out by the dealership join.
    unattr_row = (
        await session.exec(
            select(q, it, ot, rows).where(
                ApiUsage.created_at >= since_dt,
                ApiUsage.user_id.is_(None),
            )
        )
    ).one()

    def pack(row):
        quantity, input_tokens, output_tokens, n = row
        return {
            "quantity": quantity,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "rows": n,
        }

    return {
        "since": since_dt.isoformat(),
        "dealership_id": dealership_id,
        "pricing_note": "raw quantity/token sums only — no per-unit pricing in "
                        "the codebase; attach dollar rates on the frontend.",
        "total": pack(total_row),
        "by_call_type": [
            {"call_type": ct, **pack((quantity, input_tokens, output_tokens, n))}
            for ct, quantity, input_tokens, output_tokens, n in by_type_rows
        ],
        "unattributed": pack(unattr_row),
    }
