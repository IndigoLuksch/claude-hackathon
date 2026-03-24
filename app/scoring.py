"""
DarkFleet scoring engine.

Computes an IUU risk score (0–100) for a vessel given its MMSI by applying
six weighted signals against live database state.

Signal weights
--------------
+40  Encounter or transshipment event in the last 12 months
+35  AIS dark gap (> 6 h) within 50 km of an MPA (12 months)
+25  Vessel absent from rfmo_authorised for every RFMO
+20  Loitering event (< 2 kn, > 2 h) inside an MPA (12 months)
+ 7  Flag state changed more than once in the past 12 months
+ 5  Ownership opacity (Flag of Convenience or unverified registry)
+ 3  Vessel operator / flag state present in OpenSanctions dataset

Total is clamped to 100.
alert_tier: "red" >= 80 | "amber" >= 60 | "clear" < 60
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Vessel

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenSanctions — loaded once at import time
# ---------------------------------------------------------------------------

_OPENSANCTIONS_PATH = Path(os.getenv("OPENSANCTIONS_PATH", "opensanctions.json"))
_sanctioned_flags: set[str] = set()   # ISO-2 country codes, upper-cased
_sanctioned_names: set[str] = set()   # vessel / entity names, lower-cased


def _load_opensanctions() -> None:
    if not _OPENSANCTIONS_PATH.exists():
        log.warning("opensanctions.json not found at %s — sanctions signal disabled", _OPENSANCTIONS_PATH)
        return
    try:
        raw = json.loads(_OPENSANCTIONS_PATH.read_text(encoding="utf-8"))
        entities = raw if isinstance(raw, list) else raw.get("entities", [])
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            props = entity.get("properties", {})
            for flag in props.get("country", []):
                _sanctioned_flags.add(str(flag).upper())
            for name in props.get("name", []):
                _sanctioned_names.add(str(name).lower())
        log.info(
            "OpenSanctions loaded: %d flag codes, %d names",
            len(_sanctioned_flags),
            len(_sanctioned_names),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to load opensanctions.json: %s", exc)


_load_opensanctions()

# ---------------------------------------------------------------------------
# Flags of Convenience (FOC) - Heuristic for ownership opacity
# ---------------------------------------------------------------------------
_FOC_FLAGS = {
    "PA", "LR", "MH", "BS", "MT", "CY", "VG", "KY", "AG", "VC", "VU", "CK", "KM", "MD", "TG"
}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_SQL_GAP_COUNT_MPA = text("""
SELECT COUNT(*)
FROM   events e
WHERE  e.vessel_mmsi = :mmsi
  AND  e.event_type  = 'GAP'
  AND  COALESCE((e.details_json -> 'gap' ->> 'durationHours')::float, 0) > 6
  AND  e.timestamp  >= NOW() - INTERVAL '12 months'
  AND  EXISTS (
      SELECT 1 FROM mpa_zones m
      WHERE ST_DWithin(
          m.geometry::geography,
          ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326)::geography,
          50000   -- 50 km
      )
  )
""")

_SQL_LOITER_COUNT_MPA = text("""
SELECT COUNT(*)
FROM   events e
WHERE  e.vessel_mmsi = :mmsi
  AND  e.event_type  = 'LOITERING'
  AND  COALESCE((e.details_json -> 'loitering' ->> 'totalTimeHours')::float, 0) > 2
  AND  e.timestamp  >= NOW() - INTERVAL '12 months'
  AND  EXISTS (
      SELECT 1 FROM mpa_zones m
      WHERE ST_Within(
          ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326),
          m.geometry
      )
  )
""")

_SQL_ENCOUNTER_COUNT = text("""
SELECT COUNT(*)
FROM   events
WHERE  vessel_mmsi = :mmsi
  AND  event_type  IN ('ENCOUNTER', 'TRANSSHIPMENT')
  AND  timestamp  >= NOW() - INTERVAL '12 months'
""")

_SQL_RFMO_HAS_DATA = text("SELECT EXISTS (SELECT 1 FROM rfmo_authorised LIMIT 1)")

_SQL_RFMO_VESSEL = text("""
SELECT EXISTS (
    SELECT 1 FROM rfmo_authorised
    WHERE  mmsi = :mmsi
       OR  (CAST(:imo AS text) IS NOT NULL AND imo = :imo)
)
""")


async def _signal_gaps(mmsi: str, db: AsyncSession) -> tuple[float, int]:
    """35 pts per gap >6 h within 50 km of MPA (12 months), capped at 2 (70 pts max)."""
    n = int((await db.execute(_SQL_GAP_COUNT_MPA, {"mmsi": mmsi})).scalar() or 0)
    return min(n * 35, 70), n


async def _signal_loitering(mmsi: str, db: AsyncSession) -> tuple[float, int]:
    """20 pts per loitering event >2 h inside MPA (12 months), capped at 2 (40 pts max)."""
    n = int((await db.execute(_SQL_LOITER_COUNT_MPA, {"mmsi": mmsi})).scalar() or 0)
    return min(n * 20, 40), n


async def _signal_encounters(mmsi: str, db: AsyncSession) -> tuple[float, int]:
    """40 pts per encounter/transshipment in last 12 months, capped at 2 (80 pts max)."""
    n = int((await db.execute(_SQL_ENCOUNTER_COUNT, {"mmsi": mmsi})).scalar() or 0)
    return min(n * 40, 80), n


async def _signal_rfmo_absent(mmsi: str, imo: Optional[str], db: AsyncSession) -> tuple[float, str]:
    """20 pts if vessel absent from RFMO lists (10 pts if no reference data loaded)."""
    table_has_data = bool((await db.execute(_SQL_RFMO_HAS_DATA)).scalar())
    if not table_has_data:
        return 10.0, "no_data"
    authorised = bool((await db.execute(_SQL_RFMO_VESSEL, {"mmsi": mmsi, "imo": imo})).scalar())
    return (0.0, "authorised") if authorised else (20.0, "absent")


def _signal_flag_changes(flag_history: Optional[list]) -> tuple[float, int]:
    """Graduated: 1 distinct flag in 12 months = 5 pts, 2+ = 10 pts."""
    if not flag_history:
        return 0.0, 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    recent_flags: set[str] = set()
    for entry in flag_history:
        raw_date = entry.get("last_transmission") or entry.get("first_transmission")
        if not raw_date:
            continue
        try:
            dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            if dt >= cutoff:
                flag = entry.get("flag")
                if flag:
                    recent_flags.add(str(flag).upper())
        except (ValueError, TypeError):
            continue
    n = len(recent_flags)
    return (10.0 if n >= 2 else 5.0 if n == 1 else 0.0), n


def _signal_sanctions(flag_state: Optional[str], name: Optional[str]) -> float:
    if flag_state and flag_state.upper() in _sanctioned_flags:
        return 5.0
    if name and name.lower() in _sanctioned_names:
        return 5.0
    return 0.0


def _signal_ownership_opacity(flag_state: Optional[str], verified: bool) -> tuple[float, str]:
    """5 pts if FOC registry or unverified registry record."""
    if not verified:
        return 5.0, "unverified"
    if flag_state and flag_state.upper() in _FOC_FLAGS:
        return 5.0, "foc"
    return 0.0, "verified"


async def build_signal_details(vessel: "Vessel", db: AsyncSession) -> list[dict]:
    """Return per-signal breakdown for reporting and the dashboard."""
    gap_pts, gap_n = await _signal_gaps(vessel.mmsi, db)
    loiter_pts, loiter_n = await _signal_loitering(vessel.mmsi, db)
    enc_pts, enc_n = await _signal_encounters(vessel.mmsi, db)
    rfmo_pts, rfmo_status = await _signal_rfmo_absent(vessel.mmsi, vessel.imo, db)
    flag_pts, flag_n = _signal_flag_changes(vessel.flag_history_json)
    sanctions_pts = _signal_sanctions(vessel.flag_state, vessel.name)
    own_pts, own_status = _signal_ownership_opacity(vessel.flag_state, vessel.ownership_verified)

    rfmo_label = {
        "authorised": "Authorised by RFMO",
        "absent": "Absent from all RFMO lists",
        "no_data": "RFMO reference data not loaded",
    }[rfmo_status]

    own_label = {
        "unverified": "Registry record not verified via GISIS",
        "foc": "Flag of Convenience registry",
        "verified": "Registry verified",
    }[own_status]

    return [
        {
            "signal": "Encounters / transshipments (12 months)",
            "triggered": enc_n > 0,
            "points": enc_pts,
            "detail": f"{enc_n} event{'s' if enc_n != 1 else ''}" if enc_n else "",
        },
        {
            "signal": "AIS dark gaps (>6 h, near MPA)",
            "triggered": gap_n > 0,
            "points": gap_pts,
            "detail": f"{gap_n} gap{'s' if gap_n != 1 else ''} within 50km of MPA" if gap_n else "",
        },
        {
            "signal": "RFMO authorisation",
            "triggered": rfmo_pts > 0,
            "points": rfmo_pts,
            "detail": rfmo_label,
        },
        {
            "signal": "Loitering events (>2 h, inside MPA)",
            "triggered": loiter_n > 0,
            "points": loiter_pts,
            "detail": f"{loiter_n} event{'s' if loiter_n != 1 else ''} inside MPA" if loiter_n else "",
        },
        {
            "signal": "Flag state changes (12 months)",
            "triggered": flag_pts > 0,
            "points": flag_pts,
            "detail": f"{flag_n} distinct flag{'s' if flag_n != 1 else ''}" if flag_n else "",
        },
        {
            "signal": "Ownership opacity",
            "triggered": own_pts > 0,
            "points": own_pts,
            "detail": own_label,
        },
        {
            "signal": "Sanctions list match",
            "triggered": sanctions_pts > 0,
            "points": sanctions_pts,
            "detail": "",
        },
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def score_and_persist(mmsi: str, db: AsyncSession) -> tuple[float, str]:
    """
    Compute the IUU risk score for *mmsi*, persist risk_score and alert_tier
    back to the vessel row, and return (score, tier).

    The caller is responsible for committing the session.
    Returns (0.0, "clear") if the vessel is not found.
    """
    vessel: Optional[Vessel] = await db.get(Vessel, mmsi)
    if vessel is None:
        log.warning("score_and_persist: vessel %s not found", mmsi)
        return 0.0, "clear"

    gap_pts, _ = await _signal_gaps(mmsi, db)
    loiter_pts, _ = await _signal_loitering(mmsi, db)
    enc_pts, _ = await _signal_encounters(mmsi, db)
    rfmo_pts, _ = await _signal_rfmo_absent(mmsi, vessel.imo, db)
    flag_pts, _ = _signal_flag_changes(vessel.flag_history_json)
    sanctions_pts = _signal_sanctions(vessel.flag_state, vessel.name)
    own_pts, _ = _signal_ownership_opacity(vessel.flag_state, vessel.ownership_verified)
    pts: float = gap_pts + loiter_pts + enc_pts + rfmo_pts + flag_pts + sanctions_pts + own_pts

    score = min(pts, 100.0)

    if score >= 80:
        tier = "red"
    elif score >= 60:
        tier = "amber"
    else:
        tier = "clear"

    vessel.risk_score = score
    vessel.alert_tier = tier

    log.debug("scored %s → %.1f (%s)", mmsi, score, tier)
    return score, tier
