import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Vessel
from app.scoring import score_and_persist

router = APIRouter()

_running = False

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gfw_ingest.py"


@router.post("/ingest")
async def trigger_ingest(
    query: str = Query("", description="Vessel search query"),
    limit: int = Query(0, ge=0, description="Max vessels to fetch (0 = all)"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    global _running
    if _running:
        return {"status": "already_running", "vessels": 0}

    _running = True
    try:
        cmd = [sys.executable, str(_SCRIPT), "--vessel-limit", str(limit)]
        if query:
            cmd += ["--query", query]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            return {"status": "error", "detail": stderr.decode()[-500:]}

        result = await db.execute(select(Vessel.mmsi))
        mmsi_list: list[str] = result.scalars().all()
        for mmsi in mmsi_list:
            await score_and_persist(mmsi, db)
        await db.commit()

        return {"status": "ok", "vessels": len(mmsi_list)}
    finally:
        _running = False


@router.get("/ingest/status")
async def ingest_status() -> dict:
    return {"running": _running}
