"""
Shared per-user activity computation for the dashboards.

Both the manager drill-down (/manager/team/{id}, dealership-scoped) and the admin
user detail (/admin/users/{id}/activity, any user) render the SAME picture of a
salesperson — counts, favorites, and the list of cars they've made ads for — so
that logic lives here once instead of being forked per route.

Two data sources, deliberately kept distinct:
  - activity counts (generated / posted / sold) come from AdEvent, matching the
    roster / leaderboard / weekly-email definitions (posted = the three posting
    event types). These are "actions".
  - the vehicle LIST comes from the Listing table (the actual saved cars), each
    with a `posted` flag (fb_posted) + `posted_at` (fb_posted_at).

NOTE (Python 3.9): Optional[...]/List[...] only — never `X | None` (CLAUDE.md #46).
NOTE (datetime): range params are parsed to NAIVE-UTC before any comparison —
keep everything naive-UTC at the DB boundary (CLAUDE.md #24/#54/#55/#56).
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.models.ad_event import AdEvent
from app.models.listing import Listing
from app.models.user import User

# "posted" = the sum of all three posting channels — the SAME definition the
# weekly report uses (weekly_report.get_user_weekly_stats), so roster / detail /
# leaderboard never diverge.
POSTED_EVENTS = ("posted_marketplace", "posted_fb_post", "posted_fb_groups")


async def event_counts(
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


async def last_active(
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


def mode(values: List[Optional[str]]) -> Optional[str]:
    """Most common non-null value; None if none. Ties resolve to whichever `max`
    picks first — deterministic enough, never crashes."""
    vals = [v for v in values if v]
    if not vals:
        return None
    return max(set(vals), key=vals.count)


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    """ISO date/datetime → NAIVE-UTC (aware input converted to UTC then stripped).
    None/empty → None. Raises ValueError on bad input."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _strip(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a DB-read datetime to naive UTC for safe comparison — prod
    timestamptz columns come back tz-aware, dev is naive (CLAUDE.md #54)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


async def get_user_activity(
    session: SQLModelAsyncSession,
    user: User,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict:
    """
    Full activity picture for one user. `since`/`until` (naive UTC) restrict the
    VEHICLE list to cars posted (fb_posted_at) within [since, until); when both
    are None the list is all of the user's cars (posted + drafts), newest first.
    The activity counts (generated/posted/sold) are always all-time AdEvent totals
    — they're the cross-dashboard metric and are not windowed here.
    """
    uid = user.id
    counts = (await event_counts(session, [uid]))[uid]
    last = (await last_active(session, [uid]))[uid]

    # Favorites = mode of theme/format/voice across this user's 'generated' events.
    gen_rows = (
        await session.exec(
            select(AdEvent.theme_used, AdEvent.video_format, AdEvent.voice_id_used)
            .where(AdEvent.user_id == uid, AdEvent.event_type == "generated")
        )
    ).all()

    # The actual cars — from Listing, newest first.
    listings = (
        await session.exec(
            select(Listing)
            .where(Listing.user_id == uid)
            .order_by(Listing.created_at.desc())
        )
    ).all()

    # Per-channel posting breakdown, sourced from posting EVENTS (AdEvent), NOT the
    # sold-check/Listing table. This is why a vehicle posted to 3 channels shows 3
    # posts here — the sold-check side is deduped to one entry per VIN, so it could
    # never show the channel split. Events are linked to a listing via listing_id
    # (populated from the VIN at /track-posting time). Grouped once for all cars.
    EVENT_TO_CHANNEL = {
        "posted_marketplace": "marketplace",
        "posted_fb_post": "fb_post",
        "posted_fb_groups": "fb_groups",
    }
    posting_rows = (
        await session.exec(
            select(AdEvent.listing_id, AdEvent.event_type, func.count())
            .where(AdEvent.user_id == uid, AdEvent.event_type.in_(list(EVENT_TO_CHANNEL)))
            .group_by(AdEvent.listing_id, AdEvent.event_type)
        )
    ).all()
    channels_by_listing = {}
    posted_by_channel = {"marketplace": 0, "fb_post": 0, "fb_groups": 0}
    for lid, etype, cnt in posting_rows:
        ch = EVENT_TO_CHANNEL[etype]
        posted_by_channel[ch] += cnt
        if lid is not None:
            channels_by_listing.setdefault(
                lid, {"marketplace": 0, "fb_post": 0, "fb_groups": 0}
            )[ch] += cnt

    ranged = since is not None or until is not None
    vehicles = []
    for lst in listings:
        posted_at = _strip(lst.fb_posted_at)
        # When a range is active, show only cars POSTED within it (drafts have no
        # posted_at, so they're naturally excluded from a "posted in range" view).
        if ranged:
            if posted_at is None:
                continue
            if since is not None and posted_at < since:
                continue
            if until is not None and posted_at >= until:
                continue
        vehicles.append({
            "listing_id": lst.id,
            "year": lst.year,
            "make": lst.make,
            "model": lst.model,
            "price": lst.price,
            "posted": lst.fb_posted,
            "posted_at": posted_at,
            "created_at": _strip(lst.created_at),
            # which channels this specific car was posted to (from posting events)
            "channels": channels_by_listing.get(
                lst.id, {"marketplace": 0, "fb_post": 0, "fb_groups": 0}
            ),
        })

    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role,
        "dealership_id": user.dealership_id,
        "last_active": last,
        "generated": counts["generated"],
        "posted": counts["posted"],
        "sold": counts["sold"],
        "favorite_theme": mode([r[0] for r in gen_rows]),
        "favorite_format": mode([r[1] for r in gen_rows]),
        "favorite_voice": mode([r[2] for r in gen_rows]),
        "vehicles": vehicles,
        "vehicles_posted": sum(1 for v in vehicles if v["posted"]),
        # Salesperson-level channel split (all-time, from posting events). Always
        # accurate even for events not linked to a listing; sums to `posted`.
        "posted_by_channel": posted_by_channel,
        "range": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        } if ranged else None,
    }
