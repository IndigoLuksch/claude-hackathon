from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Event, Vessel, VesselOwnership

router = APIRouter()


@router.get("/vessels")
async def list_vessels(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(
        select(Vessel).order_by(Vessel.risk_score.desc()).limit(limit).offset(offset)
    )
    return [
        {
            "mmsi": v.mmsi,
            "imo": v.imo,
            "name": v.name,
            "flag_state": v.flag_state,
            "gear_type": v.gear_type,
            "last_seen": v.last_seen.isoformat() if v.last_seen else None,
            "risk_score": v.risk_score,
            "alert_tier": v.alert_tier,
        }
        for v in result.scalars().all()
    ]


@router.get("/vessels/{mmsi}/events")
async def vessel_recent_events(
    mmsi: str,
    limit: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    vessel = await db.get(Vessel, mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail=f"Vessel {mmsi} not found")

    rows = await db.execute(
        select(Event)
        .where(Event.vessel_mmsi == mmsi)
        .order_by(Event.timestamp.desc().nullslast())
        .limit(limit)
    )
    return [
        {
            "event_type": e.event_type,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "lat": e.lat,
            "lon": e.lon,
        }
        for e in rows.scalars().all()
    ]


@router.get("/vessel-trails")
async def vessel_trails(
    limit: int = Query(10, ge=2, le=50),
    db: AsyncSession = Depends(get_db),
) -> dict:
    vessels_result = await db.execute(select(Vessel))
    vessels_map = {v.mmsi: v for v in vessels_result.scalars().all()}

    events_result = await db.execute(
        select(Event)
        .where(Event.lat.isnot(None), Event.lon.isnot(None))
        .order_by(Event.vessel_mmsi, Event.timestamp.asc().nullslast())
    )

    vessel_coords: dict[str, list] = defaultdict(list)
    for e in events_result.scalars().all():
        vessel_coords[e.vessel_mmsi].append([e.lon, e.lat])

    features = []
    for mmsi, coords in vessel_coords.items():
        if len(coords) < 2:
            continue
        v = vessels_map.get(mmsi)
        if not v:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords[-limit:]},
            "properties": {
                "mmsi": mmsi,
                "alert_tier": v.alert_tier or "clear",
            },
        })

    return {"type": "FeatureCollection", "features": features}


@router.get("/vessels/{mmsi}/ownership")
async def vessel_ownership(mmsi: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(VesselOwnership)
        .where(VesselOwnership.mmsi == mmsi)
        .order_by(VesselOwnership.verified_at.desc())
        .limit(1)
    )
    ownership = result.scalar_one_or_none()
    if not ownership:
        raise HTTPException(status_code=404, detail=f"Ownership data for {mmsi} not found")

    return {
        "mmsi": ownership.mmsi,
        "imo": ownership.imo,
        "registered_owner": ownership.registered_owner,
        "registered_owner_country": ownership.registered_owner_country,
        "ship_manager": ownership.ship_manager,
        "technical_manager": ownership.technical_manager,
        "flag_state": ownership.flag_state,
        "vessel_status": ownership.vessel_status,
        "verified_at": ownership.verified_at.isoformat(),
        "source": ownership.source,
    }
