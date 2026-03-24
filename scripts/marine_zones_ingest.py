#!/usr/bin/env python3
"""
Marine Zones Ingest Script
Fetches FAO major fishing areas and EEZ boundaries from the Marine Regions
WFS service (no API key required) and loads them into mpa_zones.

Usage:
  python scripts/marine_zones_ingest.py [--fao] [--eez] [--high-seas]
  python scripts/marine_zones_ingest.py           # defaults to --fao --high-seas
"""
import asyncio
import json
import logging
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

import urllib.request
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

WFS_BASE = (
    "https://geo.vliz.be/geoserver/MarineRegions/wfs"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&outputFormat=application/json&maxFeatures=500"
    "&typeName={layer}"
)

INSERT_SQL = text(
    "INSERT INTO mpa_zones (name, geometry) "
    "VALUES (:name, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))"
)


def fetch_layer(layer: str) -> list[dict]:
    url = WFS_BASE.format(layer=layer)
    log.info("Fetching %s …", url)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp).get("features", [])


def explode_multipolygon(geom: dict) -> list[str]:
    if geom["type"] == "Polygon":
        return [json.dumps(geom)]
    if geom["type"] == "MultiPolygon":
        return [
            json.dumps({"type": "Polygon", "coordinates": ring})
            for ring in geom["coordinates"]
        ]
    return []


async def ingest_features(session, features: list[dict], name_key: str) -> int:
    count = 0
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue
        props = f.get("properties") or {}
        name = str(props.get(name_key) or props.get("name") or "Unknown Zone").strip()
        for poly_json in explode_multipolygon(geom):
            await session.execute(INSERT_SQL, {"name": name, "geom": poly_json})
            count += 1
    return count


async def main() -> None:
    parser = ArgumentParser(description="Load marine zone boundaries into DarkFleet")
    parser.add_argument("--fao", action="store_true", default=False, help="FAO major fishing areas")
    parser.add_argument("--eez", action="store_true", default=False, help="Exclusive Economic Zones")
    parser.add_argument("--high-seas", action="store_true", default=False, help="High seas pockets")
    args = parser.parse_args()

    if not any([args.fao, args.eez, args.high_seas]):
        args.fao = True
        args.high_seas = True

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    total = 0
    async with AsyncSession() as session:
        async with session.begin():
            if args.fao:
                features = fetch_layer("MarineRegions:fao")
                n = await ingest_features(session, features, "name")
                log.info("FAO fishing areas: %d polygons inserted", n)
                total += n
            if args.high_seas:
                features = fetch_layer("MarineRegions:high_seas")
                n = await ingest_features(session, features, "name")
                log.info("High seas areas: %d polygons inserted", n)
                total += n
            if args.eez:
                features = fetch_layer("MarineRegions:eez")
                n = await ingest_features(session, features, "geoname")
                log.info("EEZ zones: %d polygons inserted", n)
                total += n

    log.info("Total: %d zone polygons inserted.", total)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
