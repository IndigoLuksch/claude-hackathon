from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Vessel
from app.scoring import score_and_persist

router = APIRouter(tags=["scoring"])


# POST /score/all must be registered BEFORE /score/{mmsi} so FastAPI
# doesn't treat "all" as a path parameter.

@router.post("/score/all")
async def rescore_all_vessels(db: AsyncSession = Depends(get_db)) -> dict:
    """Rescore every vessel in the database sequentially."""
    result = await db.execute(select(Vessel.mmsi))
    mmsi_list: list[str] = result.scalars().all()

    for mmsi in mmsi_list:
        await score_and_persist(mmsi, db)

    await db.commit()
    return {"scored": len(mmsi_list)}


@router.post("/score/{mmsi}")
async def rescore_vessel(mmsi: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Rescore a single vessel and return its updated score and tier."""
    vessel = await db.get(Vessel, mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail=f"Vessel {mmsi} not found")

    score, tier = await score_and_persist(mmsi, db)
    await db.commit()

    return {"mmsi": mmsi, "risk_score": score, "alert_tier": tier}


@router.get("/alerts")
async def get_alerts(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """
    Return all vessels ordered by risk_score descending, each with their
    most recent event.
    """
    rows = await db.execute(text("""
        SELECT
            v.mmsi,
            v.imo,
            v.name,
            v.flag_state,
            v.gear_type,
            v.last_seen,
            v.risk_score,
            v.alert_tier,
            e.id          AS event_id,
            e.event_type  AS event_type,
            e.timestamp   AS event_timestamp,
            e.lat         AS event_lat,
            e.lon         AS event_lon
        FROM vessels v
        LEFT JOIN LATERAL (
            SELECT id, event_type, timestamp, lat, lon
            FROM   events
            WHERE  vessel_mmsi = v.mmsi
            ORDER  BY timestamp DESC NULLS LAST
            LIMIT  1
        ) e ON true
        ORDER BY v.risk_score DESC
    """))

    return [
        {
            "mmsi":       r.mmsi,
            "imo":        r.imo,
            "name":       r.name,
            "flag_state": r.flag_state,
            "gear_type":  r.gear_type,
            "last_seen":  r.last_seen.isoformat() if r.last_seen else None,
            "risk_score": r.risk_score,
            "alert_tier": r.alert_tier,
            "most_recent_event": {
                "id":         r.event_id,
                "event_type": r.event_type,
                "timestamp":  r.event_timestamp.isoformat() if r.event_timestamp else None,
                "lat":        r.event_lat,
                "lon":        r.event_lon,
            } if r.event_id else None,
        }
        for r in rows.mappings().all()
    ]
