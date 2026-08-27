"""
Manager Dashboard routes (Part 4).

Mounted under /manager, gated by get_current_manager (app/core/security.py,
Part 1) — role must be 'manager' AND dealership_id must be set.

HARD RULE: every query here derives the dealership scope from
current_user.dealership_id. There is deliberately NO dealership_id path/query/
body param on any of these routes — a manager can only ever see their own
dealership. If a future edit adds a dealership_id parameter to one of these
handlers, that's the exact bug get_current_manager + this module exist to
prevent.

NOTE (Python 3.9): use Optional[...]/List[...] from typing in every annotation
FastAPI/Pydantic evaluates — never `X | None` (CLAUDE.md #46).
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core.database import get_session
from app.core.security import get_current_manager
from app.models.ad_event import AdEvent
from app.models.listing import Listing
from app.models.user import User
from app.services.weekly_report import get_user_weekly_stats

router = APIRouter(prefix="/manager", tags=["manager"])

# "posted" is the sum of all three posting channels — the SAME definition the
# weekly report uses (weekly_report.get_user_weekly_stats), so roster/detail
# counts and the leaderboard never diverge.
POSTED_EVENTS = ("posted_marketplace", "posted_fb_post", "posted_fb_groups")


# ── Helpers ───────────────────────────────────────────────────
async def _event_counts(
    session: SQLModelAsyncSession, user_ids: List[int]
) -> Dict[int, Dict[str, int]]:
    """
    {user_id: {'generated': n, 'posted': n, 'sold': n}} for the given users,
    all-time, in ONE grouped query (no per-user round trip).
    """
    out: Dict[int, Dict[str, int]] = {
        uid: {"generated": 0, "posted": 0, "sold": 0} for uid in user_ids
    }
    if not user_ids:
        return out

    rows = (
        await session.exec(
            select(AdEvent.user_id, AdEvent.event_type, func.count())
            .where(AdEvent.user_id.in_(user_ids))
            .group_by(AdEvent.user_id, AdEvent.event_type)
        )
    ).all()
    for uid, event_type, cnt in rows:
        bucket = out.setdefault(uid, {"generated": 0, "posted": 0, "sold": 0})
        if event_type == "generated":
            bucket["generated"] += cnt
        elif event_type in POSTED_EVENTS:
            bucket["posted"] += cnt
        elif event_type == "sold_detected":
            bucket["sold"] += cnt
    return out


async def _last_active(
    session: SQLModelAsyncSession, user_ids: List[int]
) -> Dict[int, Optional[datetime]]:
    """{user_id: most-recent AdEvent.created_at or None}, in one grouped query."""
    out: Dict[int, Optional[datetime]] = {uid: None for uid in user_ids}
    if not user_ids:
        return out
    rows = (
        await session.exec(
            select(AdEvent.user_id, func.max(AdEvent.created_at))
            .where(AdEvent.user_id.in_(user_ids))
            .group_by(AdEvent.user_id)
        )
    ).all()
    for uid, last in rows:
        out[uid] = last
    return out


def _mode(values: List[Optional[str]]) -> Optional[str]:
    """Most common non-null value; None if there are none. Ties resolve to
    whichever `max` picks first — deterministic enough, never crashes."""
    vals = [v for v in values if v]
    if not vals:
        return None
    return max(set(vals), key=vals.count)


def _current_week_window(now: Optional[datetime] = None) -> tuple:
    """
    Current (in-progress) week: this Monday 00:00 UTC → now. NAIVE-UTC, because
    these bind as filter params against AdEvent.created_at (CLAUDE.md #24/#56 —
    keep everything naive-UTC at the DB boundary). Mirrors the SHAPE of
    weekly_report.last_week_window (Monday-anchored) but for the live week, so
    the leaderboard is "this week so far" rather than last week's finalized
    email data.
    """
    today = now or datetime.utcnow()
    week_start = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return week_start, today


def _parse_since(since: str) -> datetime:
    """Parse a `since` ISO date/datetime → NAIVE-UTC (aware input converted to
    UTC then stripped). Same rule as admin._parse_since; kept local so the two
    modules don't cross-import route internals. Raises ValueError on bad input."""
    dt = datetime.fromisoformat(since)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── STEP 1: Team roster ───────────────────────────────────────
@router.get("/team")
async def team_roster(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    manager: Annotated[User, Depends(get_current_manager)],
):
    """
    The manager's salespeople + a lifetime activity snapshot each.

    Decision — the manager is EXCLUDED from their own roster (filtered by
    User.id != manager.id). "Team roster" reads as the people you manage;
    listing yourself as a row in your own team table is confusing in a UI, and
    a manager typically doesn't generate ads. (The live leaderboard, by
    contrast, includes all staff to stay identical to the emailed one — see
    /manager/leaderboard.)
    """
    dealership_id = manager.dealership_id  # scope: from the token, never the request

    members = (
        await session.exec(
            select(User)
            .where(User.dealership_id == dealership_id, User.id != manager.id)
            .order_by(User.full_name)
        )
    ).all()
    user_ids = [u.id for u in members]

    counts = await _event_counts(session, user_ids)
    last_active = await _last_active(session, user_ids)

    return {
        "dealership_id": dealership_id,
        "count": len(members),
        "team": [
            {
                "id": u.id,
                "full_name": u.full_name,
                "email": u.email,
                "role": u.role,
                "last_active": last_active.get(u.id),
                "generated": counts[u.id]["generated"],
                "posted": counts[u.id]["posted"],
                "sold": counts[u.id]["sold"],
            }
            for u in members
        ],
    }


# ── STEP 2: Per-salesperson activity detail ───────────────────
@router.get("/team/{user_id}")
async def team_member_detail(
    user_id: int,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    manager: Annotated[User, Depends(get_current_manager)],
):
    """
    Drill-down for one salesperson in the manager's dealership.

    404 (not 403) when the user isn't in the manager's dealership — including
    when the user_id simply doesn't exist — so a manager can't probe for the
    existence of user ids outside their scope.
    """
    dealership_id = manager.dealership_id

    target = await session.get(User, user_id)
    if not target or target.dealership_id != dealership_id:
        raise HTTPException(status_code=404, detail="User not found.")

    counts = (await _event_counts(session, [user_id]))[user_id]
    last_active = (await _last_active(session, [user_id]))[user_id]

    # Favorites = mode of theme/format/voice across this user's 'generated'
    # events. Zero generated events → all three come back null (not an error).
    gen_rows = (
        await session.exec(
            select(AdEvent.theme_used, AdEvent.video_format, AdEvent.voice_id_used)
            .where(
                AdEvent.user_id == user_id,
                AdEvent.event_type == "generated",
            )
        )
    ).all()
    themes = [r[0] for r in gen_rows]
    formats = [r[1] for r in gen_rows]
    voices = [r[2] for r in gen_rows]

    return {
        "id": target.id,
        "full_name": target.full_name,
        "email": target.email,
        "role": target.role,
        "last_active": last_active,
        "generated": counts["generated"],
        "posted": counts["posted"],
        "sold": counts["sold"],
        "favorite_theme": _mode(themes),
        "favorite_format": _mode(formats),
        "favorite_voice": _mode(voices),
    }


# ── STEP 3: Live leaderboard ──────────────────────────────────
@router.get("/leaderboard")
async def leaderboard(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    manager: Annotated[User, Depends(get_current_manager)],
    since: Optional[str] = Query(
        default=None,
        description="ISO date; default = current week (this Monday 00:00 UTC → now)",
    ),
):
    """
    Live team leaderboard, scoped to the manager's dealership.

    Reuses weekly_report.get_user_weekly_stats and the SAME ranking key the
    Monday email uses (sort by total_posted desc — see
    weekly_report.format_manager_report_email), so the live and emailed
    leaderboards can't silently drift. The email is a fixed trailing Mon→Mon
    window; here `since` (default: the current in-progress week) keeps it live.

    Staff selection matches the email exactly (all Users in the dealership,
    via dealership_id) — that's the query shape the email ranks, so the manager
    themselves appears here if they have activity (intentional parity; the
    roster excludes self, this doesn't).
    """
    dealership_id = manager.dealership_id

    if since:
        try:
            week_start = _parse_since(since)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`since` must be ISO format, e.g. 2026-08-01.",
            )
        week_end = datetime.utcnow()
    else:
        week_start, week_end = _current_week_window()

    staff = (
        await session.exec(select(User).where(User.dealership_id == dealership_id))
    ).all()

    ranked = []
    for member in staff:
        stats = await get_user_weekly_stats(
            session=session,
            user_id=member.id,
            week_start=week_start,
            week_end=week_end,
        )
        ranked.append({"user": member, "stats": stats})

    # Same sort key as format_manager_report_email(): total_posted desc.
    ranked.sort(key=lambda x: x["stats"]["total_posted"], reverse=True)

    return {
        "dealership_id": dealership_id,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "since": since,
        "leaderboard": [
            {
                "rank": i + 1,
                "user_id": item["user"].id,
                "full_name": item["user"].full_name,
                "email": item["user"].email,
                "generated": item["stats"]["total_generated"],
                "posted": item["stats"]["total_posted"],
                "post_rate_pct": item["stats"]["post_rate_pct"],
                "sold": item["stats"]["vehicles_sold"],
            }
            for i, item in enumerate(ranked)
        ],
    }


# ── STEP 4: Vehicles posted vs sold ───────────────────────────
def _vehicle(listing: Listing) -> dict:
    return {
        "listing_id": listing.id,
        "year": listing.year,
        "make": listing.make,
        "model": listing.model,
        "price": listing.price,
    }


@router.get("/vehicles")
async def vehicles_posted_vs_sold(
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
    manager: Annotated[User, Depends(get_current_manager)],
):
    """
    Posted vs sold vehicles for the dealership.

    Sourced from the Listing table directly (Listing already carries the state:
    fb_posted/is_sold + their timestamps) — NOT derived from AdEvent, and no new
    column added (Listing's schema is outside Parts 1-3's scope). Scoped to
    listings whose user_id belongs to this dealership.

    Two disjoint buckets so a dashboard can show both:
      - posted  = fb_posted AND not is_sold  (live, not yet sold)
      - sold    = is_sold                     (regardless of post state)
    """
    dealership_id = manager.dealership_id

    user_ids = (
        await session.exec(
            select(User.id).where(User.dealership_id == dealership_id)
        )
    ).all()
    if not user_ids:
        return {
            "dealership_id": dealership_id,
            "posted_count": 0, "sold_count": 0,
            "posted": [], "sold": [],
        }

    listings = (
        await session.exec(select(Listing).where(Listing.user_id.in_(user_ids)))
    ).all()

    posted = [lst for lst in listings if lst.fb_posted and not lst.is_sold]
    sold = [lst for lst in listings if lst.is_sold]

    return {
        "dealership_id": dealership_id,
        "posted_count": len(posted),
        "sold_count": len(sold),
        "posted": [_vehicle(lst) for lst in posted],
        "sold": [_vehicle(lst) for lst in sold],
    }
