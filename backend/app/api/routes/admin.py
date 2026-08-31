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
from app.models.dealer_platform_domain import DealerPlatformDomain
from app.models.ad_event import AdEvent
from app.models.api_usage import ApiUsage
from app.services.analytics import PRELOADED_VOICE_IDS, record_api_usage
from app.services.send_weekly_reports import (
    send_dealership_weekly_report,
    last_week_window,
)
from app.services.user_activity import get_user_activity, parse_dt
from app.services.config_generator.claude_generator import generate_config, generate_field_selector
from app.services.email import send_dealer_config_ready_email
from bs4 import BeautifulSoup

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


class AssignToDealershipBody(BaseModel):
    dealership_id: int


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


# ── STEP 7: Per-user activity detail (same picture as the manager drill-down) ──
@router.get("/users/{user_id}/activity")
async def user_activity(
    user_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    since: Optional[str] = Query(default=None, description="ISO date; filters the vehicle list to cars posted on/after this"),
    until: Optional[str] = Query(default=None, description="ISO date; filters the vehicle list to cars posted before this"),
):
    """
    Full activity for any user — counts, favorites, and the cars they've made
    ads for. Reuses the SAME get_user_activity service the manager drill-down
    uses, so the admin and manager views of a salesperson are identical.

    Admin is not dealership-scoped, so (unlike the manager route) there's no
    dealership check — an admin may inspect any user. 404 only if the id is
    unknown.
    """
    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    try:
        since_dt, until_dt = parse_dt(since), parse_dt(until)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`since`/`until` must be ISO format, e.g. 2026-08-01.",
        )

    return await get_user_activity(session, target, since_dt, until_dt)


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


@router.get("/dealer-platforms/{platform_id}")
async def get_dealer_platform(
    platform_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Full config detail incl. config_json + the stored source_html_fragments —
    what the Config Generator loads when EDITING an existing config (e.g. an
    active one a user filed a help ticket about). 404 if unknown.
    """
    p = await session.get(DealerPlatform, platform_id)
    if not p:
        raise HTTPException(status_code=404, detail="Config not found.")
    return {
        "id": p.id,
        "name": p.name,
        "platform_slug": p.platform_slug,
        "status": p.status,
        "source_url": p.source_url,
        "config_json": p.config_json,
        "source_html_fragments": p.source_html_fragments,
        "notes": p.notes,
        "warnings": p.generation_warnings,
        "input_tokens": p.input_tokens,
        "output_tokens": p.output_tokens,
        "created_at": p.created_at,
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


@router.post("/dealer-platforms/{platform_id}/assign-to-dealership")
async def assign_platform_to_dealership(
    platform_id: int,
    payload: AssignToDealershipBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Link an approved config to a dealership: sets dealership.platform_id.

    Approval and assignment are deliberately separate steps — approve() only
    flips a config to 'active', it does NOT touch dealership rows (see its
    docstring). This route is the assignment half. It REQUIRES the platform to
    be 'active' first: assigning a still-pending or rejected config would put a
    dealership live on scraping logic no admin has vetted, so we reject that
    with a clear message rather than silently linking it.

    404 if either the platform or the dealership id doesn't exist.
    """
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    dealership = await session.get(Dealership, payload.dealership_id)
    if not dealership:
        raise HTTPException(status_code=404, detail="Dealership not found.")

    if platform.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Config {platform_id} is '{platform.status}', not 'active'. "
                "Approve it first — assignment does not skip approval."
            ),
        )

    dealership.platform_id = platform.id
    session.add(dealership)
    await session.commit()

    return {
        "message": (
            f"Config {platform_id} assigned to dealership "
            f"{dealership.id} ({dealership.dealership_name})"
        ),
        "dealership_id": dealership.id,
        "platform_id": platform.id,
        "platform_status": platform.status,
    }


# ── STEP 3: Live analytics endpoints ──────────────────────────
def _parse_since(since: Optional[str]) -> datetime:
    """
    Parse the `since` ISO date/datetime param → NAIVE-UTC datetime. Default =
    now - 7 days.

    Must be naive: it's bound as a filter against AdEvent/ApiUsage.created_at,
    which SQLAlchemy casts to TIMESTAMP WITHOUT TIME ZONE — asyncpg rejects an
    AWARE datetime there (prod-only DataError; dev SQLite tolerates it).
    CLAUDE.md bug #24. A tz-aware ISO input is converted to UTC then stripped.
    """
    if not since:
        return datetime.utcnow() - timedelta(days=7)
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`since` must be ISO format, e.g. 2026-08-01 or 2026-08-01T00:00:00.",
        )
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
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


# ══════════════════════════════════════════════════════════════
# DEALER-CONFIG-REQUEST REVIEW QUEUE  (new project — Part 2)
#
# Replaces the old semi-automatic AI-config-generation queue as the SOURCE that
# populates the admin Review Queue. Users on a paid plan submit a dealer
# inventory URL (auth.py POST /request-dealer-config → sets
# user.dealer_config_requested + _at); those requests now surface here for an
# admin to open into the Config Generator (Part 4/6).
#
# A request is "pending" when the user asked (dealer_config_requested=True) AND
# their domain does NOT already resolve to an ACTIVE DealerPlatform. Resolution
# today (Part 2) checks two sources; Part 3 adds a DealerPlatformDomain mapping
# table as the FIRST-priority source (see _resolves_to_active_config below).
# ══════════════════════════════════════════════════════════════

VALID_REQUEST_STATUS = {"pending", "approved", "all"}


def _domain_of(url: Optional[str]) -> Optional[str]:
    """
    Normalize a URL or bare domain to a lowercase bare host (no scheme, no
    leading www, no path/query). Mirrors how dealership_url is stored (bare
    domain) and how dealer_configs.get_config_for_domain parses source_url.
    """
    if not url:
        return None
    d = url.strip().lower()
    d = d.replace("https://", "").replace("http://", "")
    if d.startswith("www."):
        d = d[4:]
    d = d.split("/")[0].split("?")[0].strip()
    return d or None


def _user_config_domain(user: User) -> Optional[str]:
    """
    The domain this user's config should serve for — their registered
    dealership_url (what the extension's GET_CONFIG domain restriction keys on),
    falling back to the host of their saved dealer_inventory_url (the request
    flow only guarantees the latter is set).
    """
    return _domain_of(user.dealership_url) or _domain_of(user.dealer_inventory_url)


async def _active_config_index(session: SQLModelAsyncSession):
    """(active_domains, active_ids) computed once from all active DealerPlatforms."""
    rows = (
        await session.exec(
            select(DealerPlatform).where(DealerPlatform.status == "active")
        )
    ).all()
    domains = set()
    ids = set()
    for p in rows:
        ids.add(p.id)
        d = _domain_of(p.source_url)
        if d:
            domains.add(d)
    return domains, ids


async def _pending_config_domains(session: SQLModelAsyncSession) -> set:
    """Domains that already have a pending_review DealerPlatform (generation in
    progress) — so the admin sees a request isn't a fresh start."""
    rows = (
        await session.exec(
            select(DealerPlatform).where(DealerPlatform.status == "pending_review")
        )
    ).all()
    return {d for d in (_domain_of(p.source_url) for p in rows) if d}


async def _resolves_to_active_config(
    session: SQLModelAsyncSession,
    user: User,
    active_domains: set,
    active_ids: set,
) -> bool:
    """
    True if this user's domain already maps to an ACTIVE DealerPlatform.

    Resolution order:
      (1) DealerPlatformDomain mapping row (domain → platform_id → active) —
          the authoritative source for newly-approved shared configs (Part 3).
      (2) user's Dealership.platform_id points at an active platform, OR
      (3) an active DealerPlatform whose source_url domain == the user's domain.

    (2)/(3) stay as fallbacks so existing (un-mapped) configs keep resolving
    without a backfill.
    """
    domain = _user_config_domain(user)

    # (1) mapping table → active
    if domain:
        mapping = (
            await session.exec(
                select(DealerPlatformDomain).where(
                    DealerPlatformDomain.domain == domain
                )
            )
        ).first()
        if mapping and mapping.platform_id in active_ids:
            return True

    # (2) Dealership.platform_id → active
    if user.dealership_id is not None:
        dealership = await session.get(Dealership, user.dealership_id)
        if dealership and dealership.platform_id in active_ids:
            return True

    # (3) direct source_url domain match against active platforms
    return bool(domain and domain in active_domains)


def _serialize_request(user: User, in_progress: bool, resolved: bool) -> dict:
    return {
        "user_id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "dealership_name": user.dealership_name,
        "dealership_url": user.dealership_url,
        "dealer_inventory_url": user.dealer_inventory_url,
        "dealership_id": user.dealership_id,
        "requested_at": user.dealer_config_requested_at,
        "dealer_config_requested": user.dealer_config_requested,
        "config_domain": _user_config_domain(user),
        # Already has a pending_review DealerPlatform for this domain — admin can
        # avoid starting duplicate generation work.
        "generation_in_progress": in_progress,
        # Domain already maps to an active config (fulfilled). Pending list
        # excludes these; the 'approved' filter surfaces them.
        "resolved_to_active_config": resolved,
    }


@router.get("/dealer-config-requests")
async def list_dealer_config_requests(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="pending (default) | approved | all",
    ),
):
    """
    Populate the admin Review Queue from dealer-config requests.

    - pending (default): dealer_config_requested=True AND the domain does NOT
      resolve to an active config — the review queue.
    - approved: requested=True but the domain now resolves to an active config
      (fulfilled; flag not yet cleared — transitional view).
    - all: every user with dealer_config_requested=True.
    """
    if status_filter is not None and status_filter not in VALID_REQUEST_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(VALID_REQUEST_STATUS)}.",
        )
    mode = status_filter or "pending"

    requesters = (
        await session.exec(
            select(User)
            .where(User.dealer_config_requested == True)  # noqa: E712
            .order_by(User.dealer_config_requested_at.desc())
        )
    ).all()

    active_domains, active_ids = await _active_config_index(session)
    pending_domains = await _pending_config_domains(session)

    out = []
    for u in requesters:
        resolved = await _resolves_to_active_config(session, u, active_domains, active_ids)
        if mode == "pending" and resolved:
            continue
        if mode == "approved" and not resolved:
            continue
        domain = _user_config_domain(u)
        in_progress = bool(domain and domain in pending_domains)
        out.append(_serialize_request(u, in_progress, resolved))

    return {"count": len(out), "status": mode, "requests": out}


@router.get("/dealer-config-requests/{user_id}")
async def get_dealer_config_request(
    user_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Full detail for one request — what the Config Generator page (Part 4/6)
    loads when opened. Same shape as a list item, single object. 404 if the
    user id is unknown.
    """
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    active_domains, active_ids = await _active_config_index(session)
    pending_domains = await _pending_config_domains(session)

    resolved = await _resolves_to_active_config(session, user, active_domains, active_ids)
    domain = _user_config_domain(user)
    in_progress = bool(domain and domain in pending_domains)

    return _serialize_request(user, in_progress, resolved)


# ══════════════════════════════════════════════════════════════
# CONFIG GENERATOR  (new project — Part 4)
#
# The admin opens a pending dealer-config request (Part 2) into a generator
# page, pastes labeled HTML fragments, and generates a config. This wraps the
# existing Claude logic (config_generator.claude_generator.generate_config) —
# we assemble card_html/detail_html from the granular fields and reuse the same
# prompt rather than duplicating it. The pasted fragments are stored on the row
# (source_html_fragments) so /preview can re-run the selectors against the
# ORIGINAL HTML later (no live fetch).
# ══════════════════════════════════════════════════════════════

class ConfigGeneratorRequest(BaseModel):
    source_url: str
    # Ties this generation back to the requesting user (Part 5 approval uses it
    # to notify + set their Dealership.platform_id + clear the request flag).
    dealer_config_request_user_id: Optional[int] = None
    inventory_card_html_used: Optional[str] = None
    inventory_card_html_new: Optional[str] = None
    price_html: Optional[str] = None
    attributes_html: Optional[str] = None  # color, mileage, vin, body style, etc.
    photos_html: Optional[str] = None
    # Optional free-form guidance for obscure sites, injected into the Claude
    # prompt at highest priority (e.g. "titles are prefixed 'Pre-Owned'").
    notes_for_claude: Optional[str] = None


@router.post("/dealer-config-generator/generate")
async def generate_config_from_fragments(
    payload: ConfigGeneratorRequest,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Generate a DealerPlatform config from pasted, labeled HTML fragments.

    Reuses claude_generator.generate_config(card_html, detail_html) — we just
    assemble those two inputs from the granular fragments (labeled so Claude can
    tell them apart). Saves a new pending_review row with the raw fragments kept
    in source_html_fragments so /preview can replay the selectors.
    """
    if not (payload.inventory_card_html_used or payload.inventory_card_html_new):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one inventory card fragment (used or new) is required.",
        )

    # Strip campaign query params / fragment from the source URL so the stored
    # config + domain mapping use the clean site URL (a pasted ppc/landing link
    # like ".../?utm_source=...&gclid=..." → ".../").
    payload.source_url = (payload.source_url or "").strip().split("#")[0].split("?")[0].strip()

    # Assemble card_html (labeled) for the existing prompt.
    card_parts = []
    if payload.inventory_card_html_used:
        card_parts.append("=== USED INVENTORY CARD ===\n" + payload.inventory_card_html_used)
    if payload.inventory_card_html_new:
        card_parts.append("=== NEW INVENTORY CARD ===\n" + payload.inventory_card_html_new)
    card_html = "\n\n".join(card_parts)

    # Assemble detail_html (labeled) from price + attributes + photos.
    detail_parts = []
    if payload.price_html:
        detail_parts.append("=== PRICE SECTION ===\n" + payload.price_html)
    if payload.attributes_html:
        detail_parts.append("=== ATTRIBUTES (color / mileage / vin / body style) ===\n" + payload.attributes_html)
    if payload.photos_html:
        detail_parts.append("=== PHOTO GALLERY ===\n" + payload.photos_html)
    detail_html = "\n\n".join(detail_parts) or None

    try:
        config = await generate_config(card_html, detail_html, hints=payload.notes_for_claude)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Config generation failed: {e}",
        )

    # Clean Claude's selectors to pure CSS before storing — the extension uses
    # them raw via querySelector, which throws on annotations ("img — use
    # data-lazy…") or jQuery :contains(). _clean_selector strips annotations and
    # returns None for unusable selectors (the extension then falls back).
    _clean_config_selectors(config)

    in_tok = config.get("_usage", {}).get("input_tokens")
    out_tok = config.get("_usage", {}).get("output_tokens")

    # Keep the raw fragments (+ the request link + hints) for /preview and Part 5.
    fragments = {
        "inventory_card_html_used": payload.inventory_card_html_used,
        "inventory_card_html_new": payload.inventory_card_html_new,
        "price_html": payload.price_html,
        "attributes_html": payload.attributes_html,
        "photos_html": payload.photos_html,
        "_request_user_id": payload.dealer_config_request_user_id,
        "_notes_for_claude": payload.notes_for_claude,
    }

    platform_slug = config.get("platform", "unknown")
    platform = DealerPlatform(
        name=f"Generated: {platform_slug}",
        platform_slug=platform_slug,
        config_json=config,
        status="pending_review",
        source_url=payload.source_url,
        notes="\n".join(config.get("notes_for_human_review", [])),
        generation_warnings=config.get("_generation_warnings", []),
        input_tokens=in_tok,
        output_tokens=out_tok,
        source_html_fragments=fragments,
    )
    session.add(platform)
    await session.commit()
    await session.refresh(platform)

    # Log to api_usage so the config-generation cost shows in the admin Analytics
    # Costs panel (priced at Sonnet 4.6 rates client-side). Fire-and-forget.
    await record_api_usage(
        call_type="dealer_config_generation",
        user_id=payload.dealer_config_request_user_id,
        quantity=1,
        input_tokens=in_tok,
        output_tokens=out_tok,
        model="claude-sonnet-4-6",
    )

    # If this dealer's photos are on a Cloudflare-fronted CDN, flag the host now
    # so the very first render proxies through S3 (Shotstack is blocked by such
    # CDNs). Best-effort — never blocks generation.
    flagged_host = None
    try:
        from app.services import photo_proxy
        flagged_host = await photo_proxy.detect_and_flag_from_html(payload.photos_html)
    except Exception as e:
        print(f"[photo_proxy] config-gen detection failed: {e}")

    return {
        "platform_id": platform.id,
        "config": config,
        "status": platform.status,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "warnings": config.get("_generation_warnings", []),
        "photo_proxy_host": flagged_host,  # non-null if a Cloudflare photo host was flagged
    }


def _clean_selector(sel: Optional[str]) -> Optional[str]:
    """
    Best-effort pure CSS from a config selector value. Claude sometimes appends
    inline notes to a selector, e.g. 'img (data-src)' or
    'img.gallery-image — URL in data-src attribute'. Cut at the earliest
    annotation separator. Do NOT cut on ',' (valid CSS group) or a bare '-'
    (valid in class names) — only on a SPACED dash, which class names never
    contain.

    Returns None for jQuery-only pseudo-selectors (:contains) — those are NOT
    valid CSS and FAIL in the browser extension's document.querySelector, so we
    must not treat them as usable. The preview then falls back to a label scan
    (which is exactly what the extension does at runtime).
    """
    if not sel or not isinstance(sel, str):
        return None
    s = sel.strip()
    # em dash (—, U+2014), en dash (–, U+2013), spaced hyphen, "(" note, newline.
    seps = [" (", " —", " –", " - ", "\n"]
    cut = len(s)
    for sep in seps:
        i = s.find(sep)
        if i != -1:
            cut = min(cut, i)
    s = s[:cut].strip()
    if not s:
        return None
    # jQuery-only pseudo — unusable in the browser; reject so label-scan wins.
    if ":contains(" in s.lower():
        return None
    return s


def _clean_config_selectors(config: dict) -> None:
    """
    In-place: replace every selector string in a generated config with pure CSS
    (strip Claude's inline annotations / reject jQuery :contains). The extension
    runs these raw through querySelector, which throws on anything that isn't
    valid CSS. Leaves non-selector fields (photos_url_include, url_pattern,
    data_attribute, notes) untouched.
    """
    inv = config.get("inventory") or {}
    if isinstance(inv.get("vehicle_cards"), str):
        inv["vehicle_cards"] = _clean_selector(inv["vehicle_cards"])
    if isinstance(inv.get("button_injection"), str):
        inv["button_injection"] = _clean_selector(inv["button_injection"])
    fields = inv.get("fields") or {}
    for k, v in list(fields.items()):
        if isinstance(v, str):
            fields[k] = _clean_selector(v)
    dp = config.get("detail_page") or {}
    _keep = {"photos_url_include"}  # a URL substring, not a CSS selector
    for k, v in list(dp.items()):
        if isinstance(v, str) and k not in _keep:
            dp[k] = _clean_selector(v)


import re as _re

# dealer.com / generic spec-table label keywords, mirroring the extension's
# label-scan fallback (extractDetailPageFallback). Order = priority.
_DETAIL_LABEL_KEYWORDS = {
    "exterior_color": ["exterior color", "ext. color", "ext color", "exterior"],
    "interior_color": ["interior color", "int. color", "int color", "interior"],
    "body_style": ["body style", "body type", "bodystyle", "body"],
    "mileage": ["mileage", "odometer", "miles"],
    "vin": ["vin", "vehicle identification"],
}


def _label_scan(soup, keywords, validate=None) -> Optional[str]:
    """
    Find a value by its label, the way the extension does when a CSS selector
    returns nothing. Handles:
      - <dt>label</dt><dd>value</dd> and <th>/<td>
      - "<div class=label>Label</div><div class=detail>Value</div>" siblings
        (dealer.com features_snapshot — value cell shares its class with unrelated
        items, so only the sibling label identifies it)
      - "Label: value" text
    `validate(v)` optionally rejects wrong matches (e.g. mileage must be numeric).
    Returns the value string or None.
    """
    if soup is None:
        return None
    kws = [k.lower() for k in keywords]

    def ok(v):
        return bool(v) and (validate is None or validate(v))

    # <dt>/<dd>
    for dt in soup.find_all("dt"):
        if any(k in dt.get_text(strip=True).lower() for k in kws):
            dd = dt.find_next_sibling("dd")
            if dd and ok(dd.get_text(strip=True)):
                return dd.get_text(strip=True)
    # <th>/<td>
    for th in soup.find_all("th"):
        if any(k in th.get_text(strip=True).lower() for k in kws):
            td = th.find_next_sibling("td")
            if td and ok(td.get_text(strip=True)):
                return td.get_text(strip=True)
    # Generic: an element whose text IS the label → adjacent value (priority order)
    for k in kws:
        for el in soup.find_all(True):
            t = el.get_text(strip=True).lower().rstrip(":")
            if t != k or len(t) > 40:
                continue
            sib = el.find_next_sibling()
            while sib is not None and not sib.get_text(strip=True):
                sib = sib.find_next_sibling()
            if sib is not None and ok(sib.get_text(strip=True)):
                return sib.get_text(strip=True)
            parent = el.parent
            if parent is not None:
                for cand in parent.find_all(True):
                    if cand is el:
                        continue
                    cls = " ".join(cand.get("class", []))
                    if ("detail" in cls or "value" in cls) and ok(cand.get_text(strip=True)):
                        return cand.get_text(strip=True)
    # "Label: value" text
    for el in soup.find_all(["li", "div", "span", "p", "tr"]):
        txt = el.get_text(" ", strip=True)
        low = txt.lower()
        for k in kws:
            idx = low.find(k)
            if idx != -1:
                after = txt[idx + len(k):].lstrip(" :\t-–—")
                after = after.split("\n")[0].strip()
                if after and after.lower() != k and len(after) < 60 and ok(after):
                    return after
    return None


def _parse_title(title: Optional[str]) -> dict:
    """
    Parse 'YYYY Make Model Trim…' → {year, make, model, trim}. Mirrors the
    extension's parseYearMakeModelFromTitle (background.js): pull the year, then
    strip condition keywords ANYWHERE in the title (pre-owned / used / new /
    certified / cpo) — many sites prefix titles like 'Pre-Owned 2018 Hyundai
    Accent SE', which otherwise shifts make/model/trim. make = first token,
    model = second, trim = the rest (trim is usually the title's tail).
    """
    if not title:
        return {}
    m = _re.search(r"\b(19|20)\d{2}\b", title)
    year = m.group(0) if m else None
    rest = _re.sub(r"\b(19|20)\d{2}\b", " ", title)
    rest = _re.sub(r"\b(pre[\s-]?owned|used|new|certified|cpo)\b", " ", rest, flags=_re.I)
    rest = _re.sub(r"\s+", " ", rest).strip()
    toks = rest.split() if rest else []
    return {
        "year": year,
        "make": toks[0] if toks else None,
        "model": toks[1] if len(toks) > 1 else None,
        "trim": " ".join(toks[2:]) if len(toks) > 2 else None,
    }


def _card_title(card_ctx, fields: dict) -> Optional[str]:
    """
    Best card title to parse YMM from. Handles two layouts:
      - combined:  one element "2018 Hyundai Accent SE"
      - split:     year in one element ("2018"), name in another
                   ("Pre-Owned Hyundai Accent SE")
    Gathers candidate texts (the YMM selectors + headings), picks the one that
    parses into the most make/model, and prepends a bare-year candidate if the
    chosen name has no year of its own.
    """
    texts = []
    for key in ("year", "make", "model", "trim"):
        css = _clean_selector(fields.get(key))
        if css:
            try:
                el = card_ctx.select_one(css)
            except Exception:
                el = None
            if el:
                t = el.get_text(strip=True)
                if t:
                    texts.append(t)
    for tag in ("h1", "h2", "h3", "h4"):
        el = card_ctx.find(tag)
        if el:
            t = el.get_text(strip=True)
            if t:
                texts.append(t)
    # de-dupe, preserve order
    seen, uniq = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    if not uniq:
        for el in card_ctx.find_all(True):
            t = el.get_text(strip=True)
            if t and _re.search(r"\b(19|20)\d{2}\b", t) and len(t) < 90:
                return t
        return None

    def score(t):
        p = _parse_title(t)
        return sum(1 for k in ("make", "model") if p.get(k))

    best = max(uniq, key=score)
    # If the best name has no year but a bare-year candidate exists, combine them.
    if not _re.search(r"\b(19|20)\d{2}\b", best):
        yr = next((t for t in uniq if _re.fullmatch(r"(19|20)\d{2}", t)), None)
        if yr:
            best = f"{yr} {best}"
    return best


@router.post("/dealer-config-generator/{platform_id}/preview")
async def preview_config_against_pasted_html(
    platform_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Re-run the generated selectors against the ORIGINALLY PASTED HTML (no live
    fetch) to show sample extracted values, so the admin can sanity-check before
    approving. Best-effort: a selector that fails/matches nothing goes into
    `warnings` rather than erroring the whole preview.
    """
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    fragments = platform.source_html_fragments
    if not fragments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No stored HTML fragments for this config — regenerate via the "
                   "Config Generator to enable preview.",
        )

    config = platform.config_json or {}
    inv = config.get("inventory", {}) or {}
    fields = inv.get("fields", {}) or {}
    detail = config.get("detail_page", {}) or {}
    warnings: List[str] = []

    card_html = (
        fragments.get("inventory_card_html_used")
        or fragments.get("inventory_card_html_new")
        or ""
    )
    detail_html = "\n".join(
        p for p in [fragments.get("price_html"), fragments.get("attributes_html")] if p
    )
    photos_html = fragments.get("photos_html") or ""

    card_soup = BeautifulSoup(card_html, "html.parser")
    detail_soup = BeautifulSoup(detail_html, "html.parser")
    photos_soup = BeautifulSoup(photos_html, "html.parser") if photos_html else detail_soup

    # vehicle_cards match count (against the card fragment)
    cards = []
    cards_count = 0
    cards_css = _clean_selector(inv.get("vehicle_cards"))
    if cards_css:
        try:
            cards = card_soup.select(cards_css)
            cards_count = len(cards)
            if cards_count == 0:
                warnings.append("vehicle_cards selector matched nothing in the pasted card HTML")
        except Exception:
            warnings.append(f"vehicle_cards selector invalid: {cards_css}")
    else:
        warnings.append("vehicle_cards selector missing")

    # Context for card-relative field selectors: first matched card, else the
    # whole card fragment.
    card_ctx = cards[0] if cards else card_soup

    def by_selector(selector, contexts):
        """Return (value, cleaned_css, match_count) from the first context a valid
        selector matches; (None, css, 0) if it doesn't. match_count > 1 means the
        selector is ambiguous (e.g. a value class shared with unrelated items)."""
        css = _clean_selector(selector)
        if not css:
            return None, None, 0
        for ctx in contexts:
            try:
                els = ctx.select(css)
            except Exception:
                return None, css, 0  # invalid CSS
            if els:
                txt = els[0].get_text(strip=True)
                if txt:
                    return txt, css, len(els)
        return None, css, 0

    # ── Card title → YMM (the config often points all of year/make/model at one
    # combined title element; parse it the way the extension does at runtime).
    title = _card_title(card_ctx, fields)
    parsed = _parse_title(title)

    results = []  # each: {key,label,value,source,ok}

    def add(key, label, value, source):
        results.append({
            "key": key, "label": label,
            "value": value, "source": source, "ok": bool(value),
        })

    # Year / Make / Model / Trim — prefer a clean per-field selector value; but if
    # the selector actually yields the whole title/name (common: trim or model
    # pointed at the combined name element), use the PARSED value instead.
    def _is_full_name(v):
        if not v or not title:
            return False
        if v == title:
            return True
        mk, md = parsed.get("make"), parsed.get("model")
        return bool(mk and md and mk in v and md in v)

    for key, label in (("year", "Year"), ("make", "Make"), ("model", "Model"), ("trim", "Trim")):
        v, _css, _n = by_selector(fields.get(key), [card_ctx, card_soup])
        clean = (
            v
            and not _is_full_name(v)
            and (key != "year" or _re.fullmatch(r"(19|20)\d{2}", v or ""))
        )
        if clean:
            add(key, label, v, "selector")
        elif parsed.get(key):
            add(key, label, parsed.get(key), "title")
        else:
            add(key, label, None, "missing")

    # Price — detail sale_price, then card price selector, then label scan.
    v, _, _ = by_selector(detail.get("sale_price"), [detail_soup, card_ctx, card_soup])
    src = "selector" if v else None
    if not v:
        v, _, _ = by_selector(fields.get("price"), [card_ctx, card_soup])
        src = "selector" if v else None
    add("price", "Price", v, src or "missing")

    # Detail attributes — selector first, then label scan (mirrors the extension).
    # A selector that matches MORE THAN ONE element is ambiguous (a value class
    # shared with unrelated items, e.g. dealer.com features_snapshot) — don't
    # trust it; fall through to the label scan.
    def detail_field(key, label, selectors_contexts, validate=None):
        for sel, ctxs in selectors_contexts:
            v, _c, n = by_selector(sel, ctxs)
            if v and n == 1 and (validate is None or validate(v)):
                add(key, label, v, "selector")
                return
        scan = _label_scan(detail_soup, _DETAIL_LABEL_KEYWORDS.get(key, []), validate)
        if scan:
            add(key, label, scan, "label_scan")
        else:
            add(key, label, None, "missing")

    _has_digit = lambda v: any(c.isdigit() for c in str(v))
    detail_field("vin", "VIN", [
        (detail.get("vin"), [detail_soup]),
        (fields.get("vin"), [card_ctx, card_soup]),
    ])
    # Mileage must be numeric — reject a shared-class selector that grabbed a
    # feature name (e.g. "Premium Sound"); fall through to the label scan.
    detail_field("mileage", "Mileage", [
        (detail.get("mileage"), [detail_soup]),
        (fields.get("mileage"), [card_ctx, card_soup]),
    ], validate=_has_digit)
    detail_field("exterior_color", "Exterior color", [(detail.get("exterior_color"), [detail_soup])])
    detail_field("interior_color", "Interior color", [(detail.get("interior_color"), [detail_soup])])
    detail_field("body_style", "Body style", [(detail.get("body_style"), [detail_soup])])

    # Photos — collect the matched image URLs (so the admin can eyeball them),
    # then apply the optional URL-include filter (for sites that mix the main
    # gallery with "similar vehicles" using the same markup — a CSS selector
    # can't separate those, but a URL substring can).
    _pui_raw = (detail.get("photos_url_include") or "").strip()
    _pui_m = _re.search(r"[\w-]+(?:\.[\w-]+)+(?:/[\w./-]*)?", _pui_raw) if _pui_raw else None
    photos_url_include = (_pui_m.group(0) if _pui_m else _pui_raw)
    photo_urls = []
    photos_css = _clean_selector(detail.get("photos"))
    if photos_css:
        try:
            for img in photos_soup.select(photos_css):
                src = (img.get("data-src") or img.get("src") or "").split("?")[0]
                if src.startswith("http"):
                    photo_urls.append(src)
        except Exception:
            warnings.append(f"photos selector invalid: {photos_css}")
    # de-dupe, preserve order
    _seen = set()
    photo_urls = [u for u in photo_urls if not (u in _seen or _seen.add(u))]
    total_matched = len(photo_urls)
    if photos_url_include:
        kept = [u for u in photo_urls if photos_url_include.lower() in u.lower()]
        if kept:
            photo_urls = kept
        else:
            warnings.append(
                f"photos URL filter '{photos_url_include}' matched none of the "
                f"{total_matched} images — check the pattern."
            )
    sample_photo_count = len(photo_urls)
    if sample_photo_count == 0:
        warnings.append("photos: no images matched in the pasted gallery HTML")

    # Warn about fragile positional selectors — they can match a DIFFERENT
    # element on the full live page than on the pasted fragment (this is what
    # makes mileage come back as e.g. "Cruise Control" at runtime).
    def _fragile(css):
        c = _clean_selector(css)
        return bool(c and _re.search(r":nth-(of-type|child)|:first-child|:last-child", c))
    for lbl, sel in [
        ("Price", detail.get("sale_price")), ("VIN", detail.get("vin")),
        ("Mileage", detail.get("mileage")), ("Exterior color", detail.get("exterior_color")),
        ("Interior color", detail.get("interior_color")), ("Body style", detail.get("body_style")),
        ("Mileage (card)", fields.get("mileage")), ("VIN (card)", fields.get("vin")),
    ]:
        if _fragile(sel):
            warnings.append(
                f"{lbl} uses a positional selector ({_clean_selector(sel)}) — it may grab the "
                "wrong element on the live page. Use “Redo this field” to set a stable selector."
            )

    # Surface every still-missing field as a warning so the admin is told.
    for r in results:
        if not r["ok"]:
            warnings.append(f"{r['label']} not found — paste a narrower HTML snippet to help.")

    return {
        "platform_id": platform_id,
        "vehicle_cards_match_count": cards_count,
        "card_title": title,
        "sample_photo_count": sample_photo_count,
        "sample_photo_urls": photo_urls[:8],
        "photos_url_include": photos_url_include or None,
        "photos_total_matched": total_matched,
        "fields": results,
        "warnings": warnings,
    }


# ── Per-field refine: paste a narrower HTML snippet to derive a selector ──
# field -> (config section, config key). Card fields live under
# inventory.fields; detail fields under detail_page.
_REFINE_MAP = {
    "year": ("inventory.fields", "year"),
    "make": ("inventory.fields", "make"),
    "model": ("inventory.fields", "model"),
    "trim": ("inventory.fields", "trim"),
    "price": ("detail_page", "sale_price"),
    "vin": ("detail_page", "vin"),
    "mileage": ("detail_page", "mileage"),
    "exterior_color": ("detail_page", "exterior_color"),
    "interior_color": ("detail_page", "interior_color"),
    "body_style": ("detail_page", "body_style"),
    "photos": ("detail_page", "photos"),
    "vehicle_cards": ("inventory", "vehicle_cards"),
}
# field -> which stored fragment the pasted snippet should be merged into, so a
# later full /preview also finds it.
_REFINE_FRAGMENT = {
    "year": "inventory_card_html_used", "make": "inventory_card_html_used",
    "model": "inventory_card_html_used", "trim": "inventory_card_html_used",
    "vehicle_cards": "inventory_card_html_used",
    "price": "price_html", "photos": "photos_html",
    "vin": "attributes_html", "mileage": "attributes_html",
    "exterior_color": "attributes_html", "interior_color": "attributes_html",
    "body_style": "attributes_html",
}


class RefineFieldRequest(BaseModel):
    field: str
    html: Optional[str] = None    # required except when only setting a photos url_include
    value: Optional[str] = None   # optional hint: the exact value to target
    notes: Optional[str] = None   # optional guidance for Claude (triggers Claude mode)
    use_claude: bool = False      # force Claude even without notes
    url_include: Optional[str] = None  # photos only: keep only URLs containing this substring


def _derive_selector(html: str, value: Optional[str] = None):
    """
    Deterministically derive a CSS selector for a value from a narrow HTML
    snippet. If `value` is given, target the element whose text is (or contains)
    it; else the deepest leaf with text. Returns (selector, sample_value, warnings).
    """
    warnings: List[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    els = [e for e in soup.find_all(True) if e.get_text(strip=True)]
    if not els:
        return None, None, ["No element with text found in the pasted HTML."]

    target = None
    if value:
        v = value.strip()
        exact_leaf = [e for e in els if e.get_text(strip=True) == v and not e.find(True)]
        exact = exact_leaf or [e for e in els if e.get_text(strip=True) == v]
        contains = sorted(
            [e for e in els if v.lower() in e.get_text(strip=True).lower()],
            key=lambda e: len(e.get_text(strip=True)),
        )
        cands = exact or contains
        target = cands[0] if cands else None
        if target is None:
            warnings.append(f"Could not find '{value}' in the pasted HTML; using the deepest text element.")
    if target is None:
        leaves = [e for e in els if not e.find(True)]
        target = leaves[-1] if leaves else els[0]

    tag = target.name
    if target.get("id"):
        sel = f"{tag}#{target['id']}"
    elif target.get("class"):
        sel = tag + "." + ".".join(target["class"])
    else:
        data_attrs = [a for a in target.attrs if a.startswith("data-")]
        if data_attrs:
            sel = f"{tag}[{data_attrs[0]}]"
        else:
            sel = tag
            warnings.append(
                f"The target <{tag}> has no class/id/data attribute — using a weak "
                f"'{tag}' selector. If this is a <dt>/<dd> spec row, the extension "
                "resolves it by label text automatically at runtime."
            )
    return sel, target.get_text(strip=True), warnings


@router.post("/dealer-config-generator/{platform_id}/refine-field")
async def refine_config_field(
    platform_id: int,
    payload: RefineFieldRequest,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Redo ONE field's selector from a NARROWER HTML snippet — for a missing field
    OR to correct a false positive (a field Claude thought it got right). Two modes:

    - Deterministic (default): derive a CSS selector from the snippet; if `value`
      is given, target the element holding that value.
    - Claude-assisted (when `notes` is set or `use_claude=true`): ask Claude to
      pick the selector, guided by your notes (e.g. "body style only, ignore the
      seat count after the slash"). Use this when the value needs interpretation.

    Either way we patch the config and merge the snippet into the stored fragments
    so a later full /preview also uses it.
    """
    field = payload.field
    if field not in _REFINE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"field must be one of {sorted(_REFINE_MAP)}.",
        )

    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    # Photos URL-include filter — for sites that mix the main gallery with
    # "similar vehicles" (same markup, so no CSS selector can separate them). The
    # admin sets a URL substring only THIS car's photos contain. Deterministic;
    # no HTML/Claude needed. Optionally also merges pasted gallery HTML.
    if field == "photos" and payload.url_include is not None:
        config = dict(platform.config_json or {})
        dp = dict(config.get("detail_page", {}) or {})
        # Be forgiving if the admin typed a sentence — extract the URL-ish token
        # (e.g. "Only use photos that have imagescf.dealercenter.net/719").
        raw = payload.url_include.strip()
        m = _re.search(r"[\w-]+(?:\.[\w-]+)+(?:/[\w./-]*)?", raw)
        dp["photos_url_include"] = (m.group(0) if m else raw) or None
        config["detail_page"] = dp
        platform.config_json = config
        if (payload.html or "").strip():
            frags = dict(platform.source_html_fragments or {})
            frags["photos_html"] = ((frags.get("photos_html") or "") + "\n" + payload.html).strip()
            platform.source_html_fragments = frags
        session.add(platform)
        await session.commit()
        return {
            "platform_id": platform_id, "field": field, "selector": None,
            "url_include": dp["photos_url_include"], "ok": True, "warnings": [],
        }

    if not (payload.html or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="html is required.")

    use_claude = payload.use_claude or bool((payload.notes or "").strip())
    warnings: List[str] = []
    if use_claude:
        try:
            out = await generate_field_selector(
                field=field, html=payload.html,
                notes=payload.notes, value_hint=payload.value,
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Claude field refine failed: {e}",
            )
        selector = _clean_selector(out.get("selector"))
        sample_value = out.get("value")
        if out.get("note"):
            warnings.append(out["note"])
        usage = out.get("_usage", {}) or {}
        await record_api_usage(
            call_type="dealer_config_generation",
            user_id=(platform.source_html_fragments or {}).get("_request_user_id"),
            quantity=1,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model="claude-sonnet-4-6",
        )
        if not selector:
            return {
                "platform_id": platform_id, "field": field, "selector": None,
                "sample_value": sample_value, "ok": False,
                "warnings": warnings or ["Claude did not return a usable selector."],
            }
    else:
        selector, sample_value, warnings = _derive_selector(payload.html, payload.value)
        if not selector:
            return {
                "platform_id": platform_id, "field": field, "selector": None,
                "sample_value": None, "ok": False, "warnings": warnings,
            }

    # Patch config_json at the right path (copy so SQLAlchemy sees a new dict).
    config = dict(platform.config_json or {})
    section, key = _REFINE_MAP[field]
    if section == "inventory.fields":
        inv = dict(config.get("inventory", {}) or {})
        fld = dict(inv.get("fields", {}) or {})
        fld[key] = selector
        inv["fields"] = fld
        config["inventory"] = inv
    elif section == "inventory":
        inv = dict(config.get("inventory", {}) or {})
        inv[key] = selector
        config["inventory"] = inv
    else:  # detail_page
        dp = dict(config.get("detail_page", {}) or {})
        dp[key] = selector
        config["detail_page"] = dp
    platform.config_json = config

    # Merge the pasted snippet into the stored fragments (copy for change tracking).
    frags = dict(platform.source_html_fragments or {})
    bucket = _REFINE_FRAGMENT[field]
    frags[bucket] = ((frags.get(bucket) or "") + "\n" + payload.html).strip()
    platform.source_html_fragments = frags

    session.add(platform)
    await session.commit()

    return {
        "platform_id": platform_id,
        "field": field,
        "selector": selector,
        "sample_value": sample_value,
        "ok": True,
        "warnings": warnings,
    }


# ══════════════════════════════════════════════════════════════
# CONFIG GENERATOR — APPROVAL  (new project — Part 5)
#
# Approve a generated config in one of two modes:
#   - as a NEW config (no map_to_existing_platform_id): activate this row and
#     map the domain -> this row.
#   - MAPPED to an EXISTING active platform: reject this (duplicate) row and map
#     the domain -> the existing platform, so shared templates don't spawn
#     near-identical DealerPlatform rows.
# Either way: create/update the DealerPlatformDomain mapping, set the requesting
# user's Dealership.platform_id (if any), clear their request flag, notify them.
# ══════════════════════════════════════════════════════════════

class ApproveConfigRequest(BaseModel):
    # If set, map the domain to this already-active platform instead of the
    # newly-generated one (which gets rejected as a duplicate).
    map_to_existing_platform_id: Optional[int] = None


async def _upsert_platform_domain(
    session: SQLModelAsyncSession, domain: str, platform_id: int
) -> str:
    """Point `domain` at `platform_id` in the mapping table (unique on domain).
    Returns 'created' | 'updated'."""
    existing = (
        await session.exec(
            select(DealerPlatformDomain).where(DealerPlatformDomain.domain == domain)
        )
    ).first()
    if existing:
        existing.platform_id = platform_id
        session.add(existing)
        return "updated"
    session.add(DealerPlatformDomain(domain=domain, platform_id=platform_id))
    return "created"


@router.post("/dealer-config-generator/{platform_id}/approve")
async def approve_generated_config(
    platform_id: int,
    payload: ApproveConfigRequest,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    domain = _domain_of(platform.source_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot parse a domain from this config's source_url; "
                   "cannot create a domain mapping.",
        )

    now = datetime.utcnow()
    stamp = now.strftime("%Y-%m-%d")
    superseded = False

    if payload.map_to_existing_platform_id is not None:
        # ── MODE B: map to an existing active platform ────────────
        if payload.map_to_existing_platform_id == platform_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="map_to_existing_platform_id cannot be this same platform.",
            )
        existing = await session.get(
            DealerPlatform, payload.map_to_existing_platform_id
        )
        if not existing:
            raise HTTPException(
                status_code=404, detail="Target platform to map to not found."
            )
        if existing.status != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Target platform {existing.id} is '{existing.status}', not "
                    "'active'. You can only map a domain to an already-active platform."
                ),
            )
        # This newly-generated row is a duplicate → reject it, note why.
        platform.status = "rejected"
        platform.reviewed_at = now
        platform.reviewed_by = _admin.email
        platform.notes = (platform.notes or "") + (
            f"\n[SUPERSEDED {stamp} by {_admin.email}] "
            f"mapped to existing platform_id {existing.id}"
        )
        session.add(platform)
        resolved_platform_id = existing.id
        superseded = True
    else:
        # ── MODE A: activate this row as a new config ─────────────
        platform.status = "active"
        platform.reviewed_at = now
        platform.reviewed_by = _admin.email
        session.add(platform)
        resolved_platform_id = platform.id

    # Map the domain → resolved platform (create or update).
    mapping_action = await _upsert_platform_domain(session, domain, resolved_platform_id)

    # Tie back to the requesting user (stored on the row at generation time).
    fragments = platform.source_html_fragments or {}
    request_user_id = fragments.get("_request_user_id")

    dealership_updated = False
    user_notified = False
    notify_target = None  # (email, full_name) captured before commit

    if request_user_id:
        user = await session.get(User, request_user_id)
        if user:
            # Keep Dealership.platform_id consistent with the new mapping so the
            # existing dealership→platform resolution path agrees with the table.
            if user.dealership_id is not None:
                dealership = await session.get(Dealership, user.dealership_id)
                if dealership:
                    dealership.platform_id = resolved_platform_id
                    session.add(dealership)
                    dealership_updated = True
            # Request fulfilled → drop it off the review queue.
            user.dealer_config_requested = False
            user.updated_at = now
            session.add(user)
            notify_target = (user.email, user.full_name)

    await session.commit()

    # Email AFTER commit (fire-and-forget; never raises).
    if notify_target:
        send_dealer_config_ready_email(notify_target[0], notify_target[1], domain)
        user_notified = True

    return {
        "message": (
            f"Domain {domain} mapped to platform {resolved_platform_id}"
            + (" (superseded duplicate)" if superseded else " (activated new config)")
        ),
        "platform_id": platform_id,
        "resolved_platform_id": resolved_platform_id,
        "domain": domain,
        "mode": "mapped_existing" if superseded else "new",
        "superseded": superseded,
        "mapping": mapping_action,
        "dealership_updated": dealership_updated,
        "request_user_id": request_user_id,
        "user_notified": user_notified,
    }


# ── Assign a config directly to a salesperson (for testing / individual use) ──
class AssignToSalespersonBody(BaseModel):
    email: str


@router.post("/dealer-platforms/{platform_id}/assign-to-salesperson")
async def assign_platform_to_salesperson(
    platform_id: int,
    payload: AssignToSalespersonBody,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    _admin: Annotated[User, Depends(get_current_admin)],
):
    """
    Assign a config to an INDIVIDUAL salesperson (vs. a whole dealership) so it
    serves in that person's extension — handy for end-to-end testing.

    Works on pending OR active configs: a pending config is ACTIVATED as part of
    assigning (you're putting it into someone's hands, so it must be live). A
    rejected config is refused (409 — regenerate/approve a fresh one first).

    Effect: activates if needed, maps the config's domain -> this platform in
    DealerPlatformDomain, and sets the user's dealership_url to that domain so
    the extension's per-user domain restriction lets them receive it. Does NOT
    touch any dealership row (this is salesperson-scoped).
    """
    user = (
        await session.exec(select(User).where(User.email == payload.email.lower().strip()))
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="No user with that email.")

    platform = await session.get(DealerPlatform, platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Config not found.")

    domain = _domain_of(platform.source_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot parse a domain from this config's source_url.",
        )

    if platform.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This config is rejected — regenerate/approve a fresh one before assigning.",
        )

    activated = False
    if platform.status == "pending_review":
        platform.status = "active"
        platform.reviewed_at = datetime.utcnow()
        platform.reviewed_by = _admin.email
        session.add(platform)
        activated = True

    mapping_action = await _upsert_platform_domain(session, domain, platform.id)

    user.dealership_url = domain
    user.updated_at = datetime.utcnow()
    session.add(user)

    await session.commit()

    # Tell the salesperson their config is live (fire-and-forget; never raises).
    send_dealer_config_ready_email(user.email, user.full_name, domain)

    return {
        "message": (
            f"Assigned config {platform.id} ({domain}) to {user.email}"
            + (" — config activated" if activated else "")
        ),
        "platform_id": platform.id,
        "domain": domain,
        "user_id": user.id,
        "user_email": user.email,
        "activated": activated,
        "mapping": mapping_action,
        "status": platform.status,
    }
