import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Vessel

router = APIRouter(tags=["enrichment"])

_running = False

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "imo_gisis_enrich.py"


async def _run_enrich(mmsi: str = None, limit: int = 50):
    global _running
    if _running:
        return {"status": "already_running"}

    _running = True
    try:
        cmd = [sys.executable, str(_SCRIPT), "--limit", str(limit)]
        if mmsi:
            cmd += ["--mmsi", mmsi]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return {"status": "error", "detail": stderr.decode()[-500:]}

        return {"status": "ok", "output": stdout.decode()[-500:]}
    finally:
        _running = False


@router.post("/enrich/all")
async def enrich_all(
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """Trigger GISIS enrichment for all un-verified vessels."""
    return await _run_enrich(limit=limit)


@router.post("/enrich/{mmsi}")
async def enrich_vessel(
    mmsi: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger GISIS enrichment for a single vessel."""
    vessel = await db.get(Vessel, mmsi)
    if not vessel:
        raise HTTPException(status_code=404, detail=f"Vessel {mmsi} not found")
    if not vessel.imo:
        raise HTTPException(status_code=400, detail=f"Vessel {mmsi} has no IMO number")
    
    return await _run_enrich(mmsi=mmsi)


@router.get("/enrich/status")
async def enrich_status() -> dict:
    return {"running": _running}
