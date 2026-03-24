import json

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["mpa"])


@router.get("/mpa")
async def get_mpa_geojson(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.execute(
        text(
            """
            SELECT
                id,
                name,
                ST_AsGeoJSON(geometry) AS geometry_json
            FROM mpa_zones
            """
        )
    )
    features = []
    for row in rows.mappings().all():
        geometry_json = row["geometry_json"]
        if not geometry_json:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(geometry_json),
                "properties": {"id": row["id"], "name": row["name"]},
            }
        )
    return {"type": "FeatureCollection", "features": features}
