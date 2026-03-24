#!/usr/bin/env python3
"""
GFW Ingest Script
Calls the Global Fishing Watch API to fetch vessels and events,
then upserts results into the vessels and events tables.

Endpoints used:
  GET /v3/vessels/search  — vessel registry lookup
  GET /v3/events          — fishing, gap, encounter, loitering events

Usage:
  python scripts/gfw_ingest.py [--query SEARCH_TERM] [--vessel-limit N]

Requires GFW_API_KEY in .env (or environment).
"""
import asyncio
import logging
import os
import sys
import uuid
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import Base
from app.models import Event, Vessel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GFW_API_KEY = os.environ.get("GFW_API_KEY", "")
GFW_BASE = "https://gateway.api.globalfishingwatch.org/v3"
# v4-schema datasets required by /v3/ endpoints; old public-global-fishing-vessels
# datasets (v20201001, v20231026) are v1/v2-schema and return HTTP 422.
VESSELS_DATASET = "public-global-vessel-identity:latest"
VESSEL_TYPES = [
    # Fishing gear types
    "DRIFTING_LONGLINES",
    "TRAWLERS",
    "PURSE_SEINES",
    "SQUID_JIGS",
    "POLE_AND_LINE",
    "TROLLERS",
    "POTS_AND_TRAPS",
    "SET_LONGLINES",
    "FIXED_GEAR",
    "SET_GILLNETS",
    "DRIFT_GILLNETS",
    "LIFT_NETS",
    # Support / transshipment vessels (common IUU enablers)
    "CARRIER",
    "BUNKER",
    "SUPPORT_MOTHER_SHIP",
]
# Keep old name as alias for any external references
FISHING_GEAR_TYPES = VESSEL_TYPES
EVENTS_DATASETS = [
    "public-global-fishing-events:latest",
    "public-global-gaps-events:latest",
    "public-global-loitering-events:latest",
]
# GFW AIS data starts ~2012; use as default floor for event windows
EVENTS_FLOOR_DATE = "2012-01-01"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://darkfleet:darkfleet@localhost:5432/darkfleet",
)


def _headers() -> dict:
    return {"Authorization": f"Bearer {GFW_API_KEY}", "Content-Type": "application/json"}


async def _fetch_vessels_where(
    client: httpx.AsyncClient,
    where: str,
) -> list[dict]:
    """Paginate /v3/vessels/search using a `where` filter until exhausted (max 50/page)."""
    page_size = 50
    params: dict = {"datasets[0]": VESSELS_DATASET, "limit": page_size, "where": where}
    entries: list[dict] = []
    while True:
        r = await client.get(f"{GFW_BASE}/vessels/search", params=params, headers=_headers())
        if r.is_error:
            log.error("vessels/search %d: %s", r.status_code, r.text)
            break
        data = r.json()
        page = data.get("entries", [])
        entries.extend(page)
        since = data.get("since")
        if not since or len(page) < page_size:
            break
        params["since"] = since
    return entries


async def fetch_vessels(
    client: httpx.AsyncClient,
    query: str = "",
    limit: int = 0,
) -> list[dict]:
    """Fetch vessels. limit=0 means fetch all available."""
    if query:
        # Query mode allows up to 500 per page; paginate with `since`.
        page_size = 500
        params: dict = {"datasets[0]": VESSELS_DATASET, "limit": page_size, "query": query}
        all_entries: list[dict] = []
        while True:
            r = await client.get(f"{GFW_BASE}/vessels/search", params=params, headers=_headers())
            if r.is_error:
                log.error("vessels/search %d: %s", r.status_code, r.text)
            r.raise_for_status()
            data = r.json()
            entries = data.get("entries", [])
            all_entries.extend(entries)
            since = data.get("since")
            if not since or len(entries) < page_size:
                break
            if limit and len(all_entries) >= limit:
                break
            params["since"] = since
        return all_entries[:limit] if limit else all_entries

    # No query: exhaust each vessel type, then dedup and apply global cap.
    seen_ids: set[str] = set()
    all_entries: list[dict] = []
    for vtype in VESSEL_TYPES:
        log.info("  fetching vessel type %s…", vtype)
        entries = await _fetch_vessels_where(client, f"registryInfo.geartypes = '{vtype}'")
        added = 0
        for entry in entries:
            eid = entry.get("id") or str(
                (entry.get("registryInfo") or [{}])[0].get("ssvid")
                or (entry.get("selfReportedInfo") or [{}])[0].get("ssvid")
                or id(entry)
            )
            if eid not in seen_ids:
                seen_ids.add(eid)
                all_entries.append(entry)
                added += 1
        log.info("    → %d fetched, %d new (total unique: %d)", len(entries), added, len(all_entries))
        if limit and len(all_entries) >= limit:
            break
    return all_entries[:limit] if limit else all_entries


async def fetch_events(
    client: httpx.AsyncClient,
    gfw_vessel_ids: list[str],
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Fetch events for up to 10 vessel IDs per request (API limit).

    /v3/events requires: datasets, vessels, start-date, end-date, limit, offset.
    Event-type filtering is not supported as a query parameter in v3.
    """
    if not gfw_vessel_ids:
        return []

    all_events: list[dict] = []
    batch_size = 10
    limit = 200
    for i in range(0, len(gfw_vessel_ids), batch_size):
        batch = gfw_vessel_ids[i : i + batch_size]
        offset = 0
        while True:
            params: dict = {
                "limit": limit,
                "offset": offset,
                "start-date": start_date,
                "end-date": end_date,
            }
            for k, ds in enumerate(EVENTS_DATASETS):
                params[f"datasets[{k}]"] = ds
            for j, vid in enumerate(batch):
                params[f"vessels[{j}]"] = vid
            
            r = await client.get(f"{GFW_BASE}/events", params=params, headers=_headers())
            if r.is_error:
                log.error("events %d: %s", r.status_code, r.text)
            r.raise_for_status()
            
            entries = r.json().get("entries", [])
            all_events.extend(entries)
            
            if len(entries) < limit:
                break
            offset += limit

    return all_events


# ---- Parsers ----------------------------------------------------------------

def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_vessel(raw: dict) -> Optional[dict]:
    """Normalise a GFW v4 vessel-identity entry to our schema. Returns None if no MMSI.

    v4 schema: top-level fields are gone; data lives in registryInfo[],
    selfReportedInfo[], and combinedSourcesInfo[].
    """
    reg = (raw.get("registryInfo") or [{}])[0]
    sr = (raw.get("selfReportedInfo") or [{}])[0]
    csi = (raw.get("combinedSourcesInfo") or [{}])[0]

    mmsi = str(reg.get("ssvid") or sr.get("ssvid") or "").strip()
    if not mmsi:
        return None

    # Gear type: prefer registry, fall back to combinedSourcesInfo
    gear_type = (
        (reg.get("geartypes") or [None])[0]
        or (csi.get("geartypes") or [{}])[0].get("name")
    )

    # Last transmission: prefer registry (authoritative), fall back to AIS
    last_seen = _parse_ts(
        reg.get("transmissionDateTo") or sr.get("transmissionDateTo")
    )

    # Build flag history from all self-reported identity records
    flag_history = [
        {
            "flag": entry.get("flag"),
            "first_transmission": entry.get("transmissionDateFrom"),
            "last_transmission": entry.get("transmissionDateTo"),
        }
        for entry in (raw.get("selfReportedInfo") or [])
        if entry.get("flag")
    ] or None

    return {
        "mmsi": mmsi,
        "imo": str(reg.get("imo") or sr.get("imo") or "") or None,
        "name": str(reg.get("shipname") or sr.get("shipname") or "") or None,
        "flag_state": str(reg.get("flag") or sr.get("flag") or "") or None,
        "gear_type": str(gear_type or "") or None,
        "last_seen": last_seen,
        "risk_score": 0.0,
        "alert_tier": "clear",
        "flag_history_json": flag_history,
    }


def parse_event(raw: dict) -> dict:
    pos = raw.get("position") or {}
    vessel = raw.get("vessel") or {}
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        # v4 events: vessel.ssvid holds the MMSI
        "vessel_mmsi": str(vessel.get("ssvid") or vessel.get("id") or "") or None,
        "event_type": str(raw.get("type", "")).upper(),
        "timestamp": _parse_ts(raw.get("start")),
        "lat": pos.get("lat"),
        "lon": pos.get("lon"),
        "details_json": raw,
    }


# ---- DB helpers -------------------------------------------------------------

async def upsert_vessels(session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(Vessel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["mmsi"],
        set_={
            "imo": stmt.excluded.imo,
            "name": stmt.excluded.name,
            "flag_state": stmt.excluded.flag_state,
            "gear_type": stmt.excluded.gear_type,
            "last_seen": stmt.excluded.last_seen,
            "flag_history_json": stmt.excluded.flag_history_json,
            # risk_score and alert_tier are intentionally NOT overwritten here
        },
    )
    await session.execute(stmt)


async def upsert_events(session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(Event).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    await session.execute(stmt)


# ---- Main -------------------------------------------------------------------

async def list_datasets(client: httpx.AsyncClient) -> None:
    """Print all datasets accessible to this API key."""
    r = await client.get(
        f"{GFW_BASE}/datasets",
        params={"limit": 100, "offset": 0},
        headers=_headers(),
    )
    if r.is_error:
        log.error("datasets %d: %s", r.status_code, r.text)
        r.raise_for_status()
    data = r.json()
    entries = data if isinstance(data, list) else data.get("datasets", data.get("entries", []))
    for d in entries:
        did = d.get("id", d.get("alias", "?"))
        status = d.get("status", "")
        print(f"  {did}  [{status}]")


async def main() -> None:
    parser = ArgumentParser(description="Ingest GFW vessels and events into DarkFleet")
    parser.add_argument("--query", default="", help="Vessel search query (default: all)")
    parser.add_argument("--vessel-limit", type=int, default=0, help="Max vessels to fetch (0 = unlimited)")
    parser.add_argument("--events-start", default="", help="Events window start (YYYY-MM-DD). Defaults to each vessel's first transmission date.")
    parser.add_argument("--events-end", default="", help="Events window end (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--list-datasets", action="store_true", help="Print available datasets and exit")
    args = parser.parse_args()

    if not GFW_API_KEY:
        log.error("GFW_API_KEY is not set. Add it to .env or the environment.")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=30) as client:
        if args.list_datasets:
            print("Datasets accessible to this API key:")
            await list_datasets(client)
            return

    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    async with httpx.AsyncClient(timeout=30) as client:
        log.info("Fetching vessels (query=%r, limit=%s)…", args.query, args.vessel_limit or "unlimited")
        raw_vessels = await fetch_vessels(client, args.query, args.vessel_limit)
        log.info("Received %d vessel entries", len(raw_vessels))

        vessel_rows_raw = [r for v in raw_vessels if (r := parse_vessel(v)) is not None]
        # Deduplicate by MMSI — keep the entry with the most recent last_seen,
        # since ON CONFLICT DO UPDATE cannot update the same row twice per batch.
        _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        seen_mmsi: dict[str, dict] = {}
        for row in vessel_rows_raw:
            mmsi = row["mmsi"]
            existing = seen_mmsi.get(mmsi)
            if existing is None or (row["last_seen"] or _epoch) > (existing["last_seen"] or _epoch):
                seen_mmsi[mmsi] = row
        vessel_rows = list(seen_mmsi.values())
        log.info("Parsed %d vessels with valid MMSI (%d after dedup)", len(vessel_rows_raw), len(vessel_rows))

        # v4 schema: GFW vessel IDs live in combinedSourcesInfo[].vesselId
        gfw_ids = [
            csi["vesselId"]
            for v in raw_vessels
            for csi in (v.get("combinedSourcesInfo") or [])
            if csi.get("vesselId")
        ]

        # Derive event date window: span the full transmission history of the
        # fetched vessels so vessels active before today still get dots on the map.
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if args.events_start:
            ev_start = args.events_start
        else:
            # Earliest transmissionDateFrom across all selfReportedInfo records
            dates = [
                entry.get("transmissionDateFrom", "")[:10]
                for v in raw_vessels
                for entry in (v.get("selfReportedInfo") or [])
                if entry.get("transmissionDateFrom")
            ]
            ev_start = min(dates) if dates else EVENTS_FLOOR_DATE
            # Never go earlier than our floor date
            if ev_start < EVENTS_FLOOR_DATE:
                ev_start = EVENTS_FLOOR_DATE
        ev_end = args.events_end or today

        log.info("Fetching events for %d GFW vessel IDs (window %s → %s)…", len(gfw_ids), ev_start, ev_end)
        raw_events = await fetch_events(client, gfw_ids, ev_start, ev_end)
        log.info("Received %d event entries", len(raw_events))

        event_rows = [parse_event(e) for e in raw_events]

    async with AsyncSession() as session:
        async with session.begin():
            await upsert_vessels(session, vessel_rows)
            log.info("Upserted %d vessels", len(vessel_rows))
            await upsert_events(session, event_rows)
            log.info("Upserted %d events", len(event_rows))

    await engine.dispose()
    log.info("GFW ingest complete.")


if __name__ == "__main__":
    asyncio.run(main())
