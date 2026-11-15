"""
Dealership-owned inventory endpoints.

Prefix /dealership/inventory. Every route is gated by require_dealership_member
(active Dealership plan + a dealership_id) and scoped to current_user.dealership_id
— no route ever accepts a dealership_id from the request.

  POST /dealership/inventory/sync      upsert a page of vehicles (extension sync)
  GET  /dealership/inventory           list / search
  GET  /dealership/inventory/{id}      one vehicle (404 if not this dealership's)

No slowapi limits here (avoids bugs #70/#71). Naive UTC everywhere (bug #24).
"""
import json
import re
from datetime import datetime
from typing import List, Optional, Union
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import and_, case, func, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlalchemy.exc import IntegrityError

from app.core.database import get_session
from app.core.dealership_access import require_dealership_member
from app.models.dealership_inventory import (
    DealershipInventory,
    DealershipInventoryRead,
)
from app.models.inventory_highlights import InventoryHighlights
from app.services import highlights as highlights_svc
from app.services.body_style import normalize_body_style
from app.services.config_access import _bare_domain, get_config_access
from app.services.vin_decoder import decode_vin

router = APIRouter(prefix="/dealership/inventory", tags=["dealership-inventory"])

MAX_VEHICLES_PER_SYNC = 50
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


# ── Request / response shapes ─────────────────────────────────
class InventoryVehicleIn(BaseModel):
    # Optional so a card with no detectable VIN doesn't 422 the whole batch — the
    # route skips it with reason "invalid VIN" (via _clean_vin) instead.
    vin: Optional[str] = None
    stock_number: Optional[str] = None
    condition: Optional[str] = None
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    # price / mileage may arrive as "$27,297" / "12,345 mi" or as plain ints
    price: Optional[Union[int, str]] = None
    mileage: Optional[Union[int, str]] = None
    exterior_color: Optional[str] = None
    interior_color: Optional[str] = None
    listing_url: Optional[str] = None
    photo_urls: Optional[List[str]] = None
    carfax_url: Optional[str] = None


class InventorySyncIn(BaseModel):
    source_page_url: str
    vehicles: List[InventoryVehicleIn]


# ── Parsing helpers ───────────────────────────────────────────
def _parse_int(val) -> Optional[int]:
    """Parse '$27,297' / '12,345 mi' / 27297 → int; unparseable → None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    s = s.split(".")[0]  # whole units only; drop any decimal part
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _clean_vin(v) -> Optional[str]:
    if not v:
        return None
    vv = str(v).strip().upper()
    return vv if VIN_RE.match(vv) else None


def _strip_url(u) -> Optional[str]:
    """Drop query string + fragment (bug #65)."""
    if not u:
        return None
    s = str(u).strip()
    if not s:
        return None
    s = s.split("#", 1)[0].split("?", 1)[0]
    return s or None


def _norm_condition(c) -> Optional[str]:
    if not c:
        return None
    cc = str(c).strip().lower()
    if cc in ("new", "used"):
        return cc
    if "new" in cc:
        return "new"
    if any(k in cc for k in ("used", "pre-owned", "preowned", "cpo", "certified")):
        return "used"
    return None


def _host_of(url) -> Optional[str]:
    """Bare host (no www.) of a URL."""
    if not url:
        return None
    host = urlparse(str(url)).hostname
    if host:
        return _bare_domain(host)
    # Fallback for bare 'domain.com/...' inputs without a scheme
    return _bare_domain(url)


# ── NHTSA background fill ──────────────────────────────────────
async def _nhtsa_fill_inventory(row_ids: List[int]) -> None:
    """
    Decode each row's VIN and fill the NHTSA-derived fields. Runs after the sync
    response is sent. Fresh session per row (pipeline pattern). Failures are
    logged, never raised.
    """
    from app.core.database import AsyncSessionLocal

    for rid in row_ids:
        try:
            async with AsyncSessionLocal() as session:
                row = await session.get(DealershipInventory, rid)
                if not row or row.vehicle_data is not None:
                    continue
                vin = row.vin

            vd = await decode_vin(vin)

            async with AsyncSessionLocal() as session:
                row = await session.get(DealershipInventory, rid)
                if not row:
                    continue
                # These come only from NHTSA — set when present, don't clobber.
                row.body_style = vd.get("body_style") or row.body_style
                # Keep body_class in sync whenever body_style is (re)written.
                row.body_class = normalize_body_style(row.body_style)
                row.drivetrain = vd.get("drivetrain") or row.drivetrain
                row.fuel_type = vd.get("fuel_type") or row.fuel_type
                row.engine = vd.get("engine") or row.engine
                # year/make/model/trim only where still NULL (card value wins).
                if row.year is None and vd.get("year"):
                    try:
                        row.year = int(vd["year"])
                    except (ValueError, TypeError):
                        pass
                if row.make is None and vd.get("make"):
                    row.make = vd["make"]
                if row.model is None and vd.get("model"):
                    row.model = vd["model"]
                if row.trim is None and vd.get("trim"):
                    row.trim = vd["trim"]
                row.vehicle_data = json.dumps(vd)
                row.updated_at = datetime.utcnow()
                session.add(row)
                await session.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[inventory NHTSA fill] row {rid}: {e}")


# ── POST /sync ─────────────────────────────────────────────────
@router.post("/sync")
async def sync_inventory(
    payload: InventorySyncIn,
    background_tasks: BackgroundTasks,
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
):
    did = current_user.dealership_id

    if len(payload.vehicles) > MAX_VEHICLES_PER_SYNC:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many vehicles (max {MAX_VEHICLES_PER_SYNC}).",
        )

    # Allowed domains for this user's dealership config.
    ca = await get_config_access(current_user, session)
    allowed_bare = {
        _bare_domain(d) for d in ca["allowed_config_domains"] if _bare_domain(d)
    }

    # source_page_url domain must be allowed → else 403 for the whole request.
    page_host = _host_of(payload.source_page_url)
    if not page_host or page_host not in allowed_bare:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "DOMAIN_NOT_ALLOWED", "domain": page_host},
        )

    received = len(payload.vehicles)
    skipped: List[dict] = []
    now = datetime.utcnow()

    # First pass: validate + dedup within the batch, collect clean VINs.
    to_process = []  # (vin, vehicle_in)
    seen_vins = set()
    for v in payload.vehicles:
        vin = _clean_vin(v.vin)
        if not vin:
            skipped.append({"vin": (str(v.vin)[:32] if v.vin else ""), "reason": "invalid VIN"})
            continue
        if vin in seen_vins:
            skipped.append({"vin": vin, "reason": "duplicate VIN in batch"})
            continue
        # Per-vehicle listing_url domain check → skip (not 403) on mismatch.
        if v.listing_url:
            lhost = _host_of(v.listing_url)
            if not lhost or lhost not in allowed_bare:
                skipped.append({"vin": vin, "reason": "listing_url domain not allowed"})
                continue
        seen_vins.add(vin)
        to_process.append((vin, v))

    created = 0
    updated = 0
    upserted_rows: List[DealershipInventory] = []

    if to_process:
        # Fetch existing rows for this dealership + these VINs in one query.
        clean_vins = [vin for vin, _ in to_process]
        existing_rows = (await session.exec(
            select(DealershipInventory).where(
                DealershipInventory.dealership_id == did,
                DealershipInventory.vin.in_(clean_vins),
            )
        )).all()
        existing_by_vin = {r.vin: r for r in existing_rows}

        for vin, v in to_process:
            incoming = {
                "stock_number": v.stock_number or None,
                "condition": _norm_condition(v.condition),
                "year": v.year,
                "make": v.make or None,
                "model": v.model or None,
                "trim": v.trim or None,
                "price": _parse_int(v.price),
                "mileage": _parse_int(v.mileage),
                "exterior_color": v.exterior_color or None,
                "interior_color": v.interior_color or None,
                "listing_url": _strip_url(v.listing_url),
                "carfax_url": _strip_url(v.carfax_url),
            }
            # photo_urls: only treat a non-empty list as a value to store.
            photos_json = (
                json.dumps(v.photo_urls)
                if v.photo_urls else None
            )

            row = existing_by_vin.get(vin)
            if row is None:
                row = DealershipInventory(
                    dealership_id=did,
                    vin=vin,
                    source="extension_sync",
                    status="active",
                    synced_by_user_id=current_user.id,
                    first_seen_at=now,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                    photo_urls=photos_json,
                    **incoming,
                )
                session.add(row)
                existing_by_vin[vin] = row
                created += 1
            else:
                # Update only fields whose incoming value is non-null.
                for field, val in incoming.items():
                    if val is not None:
                        setattr(row, field, val)
                if photos_json is not None:
                    row.photo_urls = photos_json
                row.last_seen_at = now
                row.updated_at = now
                row.synced_by_user_id = current_user.id
                if row.status == "removed":
                    row.status = "active"
                    row.removed_at = None
                session.add(row)
                updated += 1
            upserted_rows.append(row)

        await session.commit()

    # Rows still missing an NHTSA decode → fill in the background.
    nhtsa_ids = [r.id for r in upserted_rows if r.vehicle_data is None and r.id is not None]
    if nhtsa_ids:
        background_tasks.add_task(_nhtsa_fill_inventory, nhtsa_ids)

    return {
        "received": received,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


# ── GET / (list / search) ──────────────────────────────────────
@router.get("")
async def list_inventory(
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
    q: Optional[str] = Query(default=None),
    condition: Optional[str] = Query(default=None),
    status_param: str = Query(default="active", alias="status"),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
):
    did = current_user.dealership_id
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)

    filters = [DealershipInventory.dealership_id == did]
    if status_param:
        filters.append(DealershipInventory.status == status_param)
    if condition:
        nc = _norm_condition(condition)
        if nc:
            filters.append(DealershipInventory.condition == nc)

    rank_expr = None
    if q and q.strip():
        qs = q.strip()
        qup = qs.upper()
        matchers = []
        rank_whens = []

        # 1) exact stock number
        stock_match = func.upper(func.coalesce(DealershipInventory.stock_number, "")) == qup
        matchers.append(stock_match)
        rank_whens.append((stock_match, 0))

        # 2) VIN: full match or suffix of >= 6 chars
        if re.match(r"^[A-HJ-NPR-Z0-9]+$", qup) and len(qup) >= 6:
            vin_match = func.upper(DealershipInventory.vin).like(f"%{qup}")
            matchers.append(vin_match)
            rank_whens.append((vin_match, 1))

        # 3) keyword ILIKE across year/make/model/trim — every term must match
        terms = qs.split()
        if terms:
            haystack = func.concat_ws(
                " ",
                func.cast(DealershipInventory.year, sa.String),
                DealershipInventory.make,
                DealershipInventory.model,
                DealershipInventory.trim,
            )
            kw_match = and_(*[haystack.ilike(f"%{t}%") for t in terms])
            matchers.append(kw_match)
            rank_whens.append((kw_match, 2))

        filters.append(or_(*matchers))
        rank_expr = case(*rank_whens, else_=3)

    # total (count of the filtered set)
    total = (await session.exec(
        select(func.count()).select_from(DealershipInventory).where(*filters)
    )).one()

    # page of rows
    stmt = select(DealershipInventory).where(*filters)
    if rank_expr is not None:
        stmt = stmt.order_by(rank_expr, DealershipInventory.last_seen_at.desc())
    else:
        stmt = stmt.order_by(DealershipInventory.last_seen_at.desc())
    stmt = stmt.offset(offset).limit(limit)

    rows = (await session.exec(stmt)).all()
    items = [DealershipInventoryRead.model_validate(r) for r in rows]
    return {"items": items, "total": total}


# ── GET /{id} ──────────────────────────────────────────────────
@router.get("/{inventory_id}", response_model=DealershipInventoryRead)
async def get_inventory_item(
    inventory_id: int,
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(DealershipInventory, inventory_id)
    if not row or row.dealership_id != current_user.dealership_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found.",
        )
    return row


# ── PUT /{id}/specs ────────────────────────────────────────────
# Detail-page (VDP) specs captured by the extension (session 2).
MAX_SPEC_ROWS = 100
MAX_HIGHLIGHTS = 60
MAX_PACKAGES = 30
MAX_PACKAGE_ITEMS = 60
STR_CAP = 200


class PackageIn(BaseModel):
    name: Optional[str] = None
    items: Optional[List[str]] = None


class SpecsIn(BaseModel):
    source_page_url: str
    spec_table: Optional[dict] = None          # {label: value}
    highlights: Optional[List[str]] = None
    packages: Optional[List[PackageIn]] = None
    window_sticker_url: Optional[str] = None
    carfax_url: Optional[str] = None
    stock_number: Optional[str] = None
    exterior_color: Optional[str] = None
    interior_color: Optional[str] = None
    price: Optional[Union[int, str]] = None
    mileage: Optional[Union[int, str]] = None


def _cap_str(v) -> Optional[str]:
    """Trim + cap a string at STR_CAP chars. None/empty → None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:STR_CAP]


@router.put("/{inventory_id}/specs", response_model=DealershipInventoryRead)
async def put_specs(
    inventory_id: int,
    payload: SpecsIn,
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(DealershipInventory, inventory_id)
    if not row or row.dealership_id != current_user.dealership_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found.",
        )

    # ── Size limits → 422 ──────────────────────────────────────
    spec_table = payload.spec_table or {}
    highlights = payload.highlights or []
    packages = payload.packages or []
    if len(spec_table) > MAX_SPEC_ROWS:
        raise HTTPException(422, detail=f"Too many spec rows (max {MAX_SPEC_ROWS}).")
    if len(highlights) > MAX_HIGHLIGHTS:
        raise HTTPException(422, detail=f"Too many highlights (max {MAX_HIGHLIGHTS}).")
    if len(packages) > MAX_PACKAGES:
        raise HTTPException(422, detail=f"Too many packages (max {MAX_PACKAGES}).")
    for p in packages:
        if p.items and len(p.items) > MAX_PACKAGE_ITEMS:
            raise HTTPException(
                422, detail=f"Too many items in a package (max {MAX_PACKAGE_ITEMS}).")

    # ── Domain checks → 403 ────────────────────────────────────
    ca = await get_config_access(current_user, session)
    allowed_bare = {
        _bare_domain(d) for d in ca["allowed_config_domains"] if _bare_domain(d)
    }
    page_host = _host_of(payload.source_page_url)
    if not page_host or page_host not in allowed_bare:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "DOMAIN_NOT_ALLOWED", "domain": page_host},
        )
    # source_page_url host must match the row's listing_url host.
    row_host = _host_of(row.listing_url)
    if not row_host or page_host != row_host:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "URL_MISMATCH", "page_host": page_host, "row_host": row_host},
        )

    # ── Build specs_json (trim + cap strings) ──────────────────
    clean_spec_table = {}
    for k, v in spec_table.items():
        ck = _cap_str(k)
        cv = _cap_str(v)
        if ck is not None and cv is not None:
            clean_spec_table[ck] = cv
    clean_highlights = [h for h in (_cap_str(x) for x in highlights) if h]
    clean_packages = []
    for p in packages:
        pname = _cap_str(p.name)
        pitems = [i for i in (_cap_str(x) for x in (p.items or [])) if i]
        if pname or pitems:
            clean_packages.append({"name": pname, "items": pitems})

    specs_obj = {
        "spec_table": clean_spec_table,
        "highlights": clean_highlights,
        "packages": clean_packages,
        "window_sticker_url": _cap_str(payload.window_sticker_url),
    }
    row.specs_json = json.dumps(specs_obj)
    row.specs_source = "detail_page"
    row.specs_captured_at = datetime.utcnow()

    # ── Merge non-null scalar fields (non-null wins, like sync) ─
    cx = _cap_str(payload.carfax_url)
    if cx is not None:
        row.carfax_url = _strip_url(cx)
    sn = _cap_str(payload.stock_number)
    if sn is not None:
        row.stock_number = sn
    ec = _cap_str(payload.exterior_color)
    if ec is not None:
        row.exterior_color = ec
    ic = _cap_str(payload.interior_color)
    if ic is not None:
        row.interior_color = ic
    pr = _parse_int(payload.price)
    if pr is not None:
        row.price = pr
    mi = _parse_int(payload.mileage)
    if mi is not None:
        row.mileage = mi

    row.updated_at = datetime.utcnow()
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ── GET /{id}/highlights ───────────────────────────────────────
@router.get("/{inventory_id}/highlights")
async def get_highlights(
    inventory_id: int,
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
    language: Optional[str] = Query(default=None),
):
    row = await session.get(DealershipInventory, inventory_id)
    if not row or row.dealership_id != current_user.dealership_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found.",
        )

    lang = language or current_user.highlights_language or "en"
    if lang not in ("en", "es"):
        lang = "en"

    want_hash = highlights_svc.compute_input_hash(row, lang)
    dq = highlights_svc.data_quality(row)

    # Cache hit: existing row for (inventory_id, language) with the same hash.
    existing = (await session.exec(
        select(InventoryHighlights).where(
            InventoryHighlights.inventory_id == row.id,
            InventoryHighlights.language == lang,
        )
    )).first()
    if existing and existing.input_hash == want_hash:
        return {
            "highlights": json.loads(existing.highlights_json or "[]"),
            "language": lang,
            "generated_at": existing.updated_at.isoformat(),
            "data_quality": dq,
            "cached": True,
        }

    # Generate (never cache a failure → 502).
    try:
        result = await highlights_svc.generate_highlights(row, lang, current_user.id)
    except highlights_svc.HighlightsUnavailable:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "HIGHLIGHTS_UNAVAILABLE"},
        )

    highlights_json = json.dumps(result["highlights"])
    now = datetime.utcnow()

    if existing:
        existing.highlights_json = highlights_json
        existing.input_hash = want_hash
        existing.model = result["model"]
        existing.updated_at = now
        session.add(existing)
        await session.commit()
        await session.refresh(existing)
        saved = existing
    else:
        saved = InventoryHighlights(
            inventory_id=row.id,
            language=lang,
            highlights_json=highlights_json,
            input_hash=want_hash,
            model=result["model"],
            created_at=now,
            updated_at=now,
        )
        session.add(saved)
        try:
            await session.commit()
            await session.refresh(saved)
        except IntegrityError:
            # Concurrency: another rep inserted the same (inventory_id, language)
            # first. Re-read theirs instead of double-inserting.
            await session.rollback()
            saved = (await session.exec(
                select(InventoryHighlights).where(
                    InventoryHighlights.inventory_id == row.id,
                    InventoryHighlights.language == lang,
                )
            )).first()
            if saved is None:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"code": "HIGHLIGHTS_UNAVAILABLE"},
                )

    return {
        "highlights": json.loads(saved.highlights_json or "[]"),
        "language": lang,
        "generated_at": saved.updated_at.isoformat(),
        "data_quality": dq,
        "cached": False,
    }


# ── GET /{id}/alternatives ─────────────────────────────────────
@router.get("/{inventory_id}/alternatives")
async def get_alternatives(
    inventory_id: int,
    current_user=Depends(require_dealership_member),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=5),
):
    row = await session.get(DealershipInventory, inventory_id)
    if not row or row.dealership_id != current_user.dealership_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found.",
        )

    limit = min(max(limit, 1), 10)

    if not row.body_class:
        return {"items": [], "reason": "no_body_class"}
    if row.price is None:
        return {"items": [], "reason": "no_price"}

    filters = [
        DealershipInventory.dealership_id == current_user.dealership_id,
        DealershipInventory.status == "active",
        DealershipInventory.id != row.id,
        DealershipInventory.body_class == row.body_class,
        DealershipInventory.price.is_not(None),
        DealershipInventory.price >= row.price * 0.9,
        DealershipInventory.price <= row.price * 1.1,
    ]
    if row.year is not None:
        filters.append(DealershipInventory.year >= row.year - 2)
        filters.append(DealershipInventory.year <= row.year + 2)

    candidates = (await session.exec(
        select(DealershipInventory).where(*filters)
    )).all()

    scored = []
    for c in candidates:
        price_diff = (c.price - row.price) if c.price is not None else None
        year_diff = (c.year - row.year) if (c.year is not None and row.year is not None) else None
        mileage_diff = (
            (c.mileage - row.mileage)
            if (c.mileage is not None and row.mileage is not None) else None
        )
        # Closeness score — lower is closer. NULL mileage = neutral (0).
        score = abs(price_diff) / row.price if price_diff is not None else 0.0
        if year_diff is not None:
            score += abs(year_diff) / 5.0
        if mileage_diff is not None:
            score += abs(mileage_diff) / 50000.0
        item = DealershipInventoryRead.model_validate(c).model_dump()
        item["price_diff"] = price_diff
        item["year_diff"] = year_diff
        item["mileage_diff"] = mileage_diff
        scored.append((score, item))

    scored.sort(key=lambda t: t[0])
    return {"items": [it for _, it in scored[:limit]], "reason": None}
