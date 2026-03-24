#!/usr/bin/env python3
"""
RFMO Ingest Script
Reads WCPFC and/or ICCAT authorised-vessel CSVs and loads them into rfmo_authorised.

Usage:
  python scripts/rfmo_ingest.py --wcpfc path/to/wcpfc.csv --iccat path/to/iccat.csv
  python scripts/rfmo_ingest.py wcpfc.csv iccat.csv         # positional: WCPFC first, ICCAT second
  python scripts/rfmo_ingest.py --wcpfc wcpfc.csv           # single RFMO is fine

Column mapping is flexible — see WCPFC_COLS / ICCAT_COLS below.
Add or adjust candidate column names to match the headers in your actual CSV files.
"""
import asyncio
import csv
import logging
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import Base
from app.models import RFMOAuthorised

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://darkfleet:darkfleet@localhost:5432/darkfleet",
)

# Candidate column names for each field, tried in order.
WCPFC_COLS: dict[str, list[str]] = {
    "mmsi": ["MMSI", "mmsi", "Mmsi"],
    "imo": ["IMO", "imo", "IMO_Number", "IMO Number"],
    "authorised_species": ["Species", "SPECIES", "Authorised_Species", "TARGET_SPECIES"],
    "authorised_zone": ["Zone", "ZONE", "Area", "AREA", "Convention_Area"],
}

ICCAT_COLS: dict[str, list[str]] = {
    "mmsi": ["MMSI", "mmsi", "Mmsi"],
    "imo": ["IMO", "imo", "IMO Number", "IMO_Number"],
    "authorised_species": ["Species", "SPECIES", "Species Name"],
    "authorised_zone": ["Zone", "ZONE", "Area", "Convention Area", "Convention_Area"],
}


def _pick(row: dict, candidates: list[str]) -> str | None:
    """Return the first candidate column value found in row, or None."""
    for key in candidates:
        if key in row and row[key].strip():
            return row[key].strip()
    return None


def parse_csv(path: str, rfmo_name: str, col_map: dict[str, list[str]]) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "mmsi": _pick(row, col_map["mmsi"]),
                    "imo": _pick(row, col_map["imo"]),
                    "rfmo_name": rfmo_name,
                    "authorised_species": _pick(row, col_map["authorised_species"]),
                    "authorised_zone": _pick(row, col_map["authorised_zone"]),
                }
            )
    return rows


async def main() -> None:
    parser = ArgumentParser(description="Load RFMO authorised vessel CSVs into DarkFleet")
    parser.add_argument("--wcpfc", metavar="FILE", help="Path to WCPFC authorised vessel CSV")
    parser.add_argument("--iccat", metavar="FILE", help="Path to ICCAT authorised vessel CSV")
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="Positional fallback: first file = WCPFC, second = ICCAT",
    )
    args = parser.parse_args()

    wcpfc_path = args.wcpfc or (args.files[0] if len(args.files) > 0 else None)
    iccat_path = args.iccat or (args.files[1] if len(args.files) > 1 else None)

    if not wcpfc_path and not iccat_path:
        parser.error("Provide at least one CSV via --wcpfc or --iccat (or as positional args).")

    all_rows: list[dict] = []

    if wcpfc_path:
        log.info("Parsing WCPFC CSV: %s", wcpfc_path)
        wcpfc_rows = parse_csv(wcpfc_path, "WCPFC", WCPFC_COLS)
        log.info("  %d rows", len(wcpfc_rows))
        all_rows.extend(wcpfc_rows)

    if iccat_path:
        log.info("Parsing ICCAT CSV: %s", iccat_path)
        iccat_rows = parse_csv(iccat_path, "ICCAT", ICCAT_COLS)
        log.info("  %d rows", len(iccat_rows))
        all_rows.extend(iccat_rows)

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession() as session:
        async with session.begin():
            for row in all_rows:
                session.add(RFMOAuthorised(**row))

    log.info("Inserted %d RFMO authorised vessel records.", len(all_rows))
    await engine.dispose()
    log.info("RFMO ingest complete.")


if __name__ == "__main__":
    asyncio.run(main())
