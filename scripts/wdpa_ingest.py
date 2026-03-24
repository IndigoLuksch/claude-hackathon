#!/usr/bin/env python3
"""
WDPA Ingest Script
Reads a WDPA GeoJSON file and loads MPA polygons into mpa_zones
using PostGIS ST_GeomFromGeoJSON / ST_SetSRID.

MultiPolygon features are split into individual Polygon rows (one per part),
so all rows in mpa_zones carry a true Polygon geometry.

Usage:
  python scripts/wdpa_ingest.py path/to/wdpa.geojson
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import Base

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://darkfleet:darkfleet@localhost:5432/darkfleet",
)

# Candidate property keys for the MPA name, tried in order.
NAME_FIELDS = ["NAME", "name", "ORIG_NAME", "WDPA_PID", "SITE_NAME"]

INSERT_SQL = text(
    "INSERT INTO mpa_zones (name, geometry) "
    "VALUES (:name, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))"
)


def get_name(props: dict) -> str:
    for field in NAME_FIELDS:
        if props.get(field):
            return str(props[field]).strip()
    return "Unknown MPA"


def extract_polygons(geom: dict) -> list[str]:
    """
    Return a list of GeoJSON Polygon strings extracted from a geometry.
    MultiPolygon is split into individual polygons.
    Other geometry types are skipped (returns empty list).
    """
    gtype = geom.get("type", "")

    if gtype == "Polygon":
        return [json.dumps(geom)]

    if gtype == "MultiPolygon":
        return [
            json.dumps({"type": "Polygon", "coordinates": ring})
            for ring in geom.get("coordinates", [])
        ]

    return []  # GeometryCollection, Point, LineString etc. are not stored


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/wdpa_ingest.py <path_to_wdpa.geojson>", file=sys.stderr)
        sys.exit(1)

    geojson_path = sys.argv[1]
    log.info("Loading WDPA GeoJSON: %s", geojson_path)

    with open(geojson_path, encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
    elif data.get("type") == "Feature":
        features = [data]
    else:
        log.error("Unexpected GeoJSON type: %s", data.get("type"))
        sys.exit(1)

    log.info("Found %d features", len(features))

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    inserted = 0
    skipped = 0

    async with AsyncSession() as session:
        async with session.begin():
            for feature in features:
                geom = feature.get("geometry")
                props = feature.get("properties") or {}

                if not geom:
                    skipped += 1
                    continue

                polygons = extract_polygons(geom)
                if not polygons:
                    log.debug(
                        "Skipping unsupported geometry type '%s' for feature '%s'",
                        geom.get("type"),
                        get_name(props),
                    )
                    skipped += 1
                    continue

                name = get_name(props)
                for poly_json in polygons:
                    await session.execute(INSERT_SQL, {"name": name, "geom": poly_json})
                    inserted += 1

    log.info("Inserted %d MPA zone rows, skipped %d features.", inserted, skipped)
    await engine.dispose()
    log.info("WDPA ingest complete.")


if __name__ == "__main__":
    asyncio.run(main())
