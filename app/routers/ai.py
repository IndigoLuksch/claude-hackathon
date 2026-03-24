"""
AI-powered maritime intelligence layer.

Provides two endpoints:
  POST /ai/brief/{mmsi}  — auto-generated intelligence brief for a vessel
  POST /ai/chat          — conversational follow-up with streaming
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Event, Vessel, VesselOwnership
from app.scoring import build_signal_details

log = logging.getLogger(__name__)
router = APIRouter(tags=["ai"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"

# Cache briefs in memory (mmsi -> {brief, generated_at})
_brief_cache: dict[str, dict] = {}
_CACHE_TTL = timedelta(minutes=10)

SYSTEM_PROMPT = """You are DarkFleet Intelligence Analyst, an expert AI maritime analyst specializing in Illegal, Unreported, and Unregulated (IUU) fishing detection.

Your role is to help human analysts, regulators, and enforcement agencies understand vessel risk profiles and make informed decisions about which vessels to investigate.

You have deep knowledge of:
- IUU fishing tactics: AIS manipulation (dark gaps), transshipment at sea, flag-hopping, use of Flags of Convenience
- Maritime law: RFMO regulations, port state measures, UNCLOS provisions
- Vessel identification: MMSI/IMO numbering, ownership opacity, shell companies
- Marine Protected Areas and their significance for biodiversity

When analyzing a vessel, you should:
1. Synthesize all available signals into a coherent narrative
2. Explain WHY each signal matters (not just that it exists)
3. Identify patterns that suggest intentional evasion vs. normal operations
4. Recommend specific next steps for human investigators
5. Flag what you DON'T know and what additional data would help

Always be clear about uncertainty. Never claim a vessel is definitively engaged in IUU fishing — present evidence and let humans decide. Your job is to EMPOWER human decision-making, not replace it.

Respond in clear, professional language suitable for a regulatory briefing. Use markdown formatting for readability."""


async def _gather_vessel_context(mmsi: str, db: AsyncSession) -> dict:
    """Gather all available data for a vessel into a context dict."""
    vessel = await db.get(Vessel, mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail=f"Vessel {mmsi} not found")

    # Recent events (last 20)
    event_rows = await db.execute(
        select(Event)
        .where(Event.vessel_mmsi == mmsi)
        .order_by(Event.timestamp.desc().nullslast())
        .limit(20)
    )
    events = [
        {
            "type": e.event_type,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "lat": e.lat,
            "lon": e.lon,
        }
        for e in event_rows.scalars().all()
    ]

    # Scoring signals
    signals = await build_signal_details(vessel, db)

    # Ownership
    ownership_row = await db.execute(
        select(VesselOwnership)
        .where(VesselOwnership.mmsi == mmsi)
        .order_by(VesselOwnership.verified_at.desc())
        .limit(1)
    )
    ownership = ownership_row.scalar_one_or_none()

    return {
        "vessel": {
            "mmsi": vessel.mmsi,
            "imo": vessel.imo,
            "name": vessel.name,
            "flag_state": vessel.flag_state,
            "gear_type": vessel.gear_type,
            "last_seen": vessel.last_seen.isoformat() if vessel.last_seen else None,
            "risk_score": vessel.risk_score,
            "alert_tier": vessel.alert_tier,
            "iuu_blacklisted": vessel.iuu_blacklisted,
            "detained_24m": vessel.detained_24m,
            "ownership_verified": vessel.ownership_verified,
            "flag_history": vessel.flag_history_json,
        },
        "events": events,
        "signals": signals,
        "ownership": {
            "registered_owner": ownership.registered_owner if ownership else None,
            "registered_owner_country": ownership.registered_owner_country if ownership else None,
            "ship_manager": ownership.ship_manager if ownership else None,
            "flag_state": ownership.flag_state if ownership else None,
            "source": ownership.source if ownership else None,
            "verified_at": ownership.verified_at.isoformat() if ownership and ownership.verified_at else None,
        } if ownership else None,
    }


def _build_vessel_context_message(ctx: dict) -> str:
    """Format vessel context into a user message for Claude."""
    v = ctx["vessel"]
    lines = [
        f"## Vessel Under Analysis",
        f"- **Name:** {v['name'] or 'Unknown'}",
        f"- **MMSI:** {v['mmsi']}",
        f"- **IMO:** {v['imo'] or 'N/A'}",
        f"- **Flag State:** {v['flag_state'] or 'Unknown'}",
        f"- **Gear Type:** {v['gear_type'] or 'Unknown'}",
        f"- **Last Seen:** {v['last_seen'] or 'Unknown'}",
        f"- **Risk Score:** {v['risk_score']}/100",
        f"- **Alert Tier:** {v['alert_tier']}",
        f"- **IUU Blacklisted:** {v['iuu_blacklisted']}",
        f"- **Detained (24 months):** {v['detained_24m']}",
        f"- **Ownership Verified:** {v['ownership_verified']}",
    ]

    if v.get("flag_history"):
        lines.append(f"- **Flag History:** {json.dumps(v['flag_history'])}")

    if ctx.get("ownership"):
        o = ctx["ownership"]
        lines.append(f"\n## Ownership Record")
        lines.append(f"- **Registered Owner:** {o.get('registered_owner') or 'Unknown'}")
        lines.append(f"- **Owner Country:** {o.get('registered_owner_country') or 'Unknown'}")
        lines.append(f"- **Ship Manager:** {o.get('ship_manager') or 'Unknown'}")
        lines.append(f"- **Source:** {o.get('source') or 'N/A'}")

    lines.append(f"\n## Risk Signals")
    for s in ctx["signals"]:
        status = "TRIGGERED" if s["triggered"] else "clear"
        lines.append(f"- [{status}] {s['signal']}: {s['points']} pts — {s.get('detail', '')}")

    lines.append(f"\n## Recent Events ({len(ctx['events'])} most recent)")
    for e in ctx["events"][:10]:
        lines.append(f"- {e['type']} at {e['timestamp'] or '?'} ({e['lat']}, {e['lon']})")

    return "\n".join(lines)


@router.post("/ai/brief/{mmsi}")
async def generate_brief(mmsi: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Generate an AI intelligence brief for a vessel."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    # Check cache
    cached = _brief_cache.get(mmsi)
    if cached and (datetime.now(timezone.utc) - cached["generated_at"]) < _CACHE_TTL:
        return cached["data"]

    ctx = await _gather_vessel_context(mmsi, db)
    context_msg = _build_vessel_context_message(ctx)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"{context_msg}\n\nProvide a concise intelligence brief for this vessel. Structure your response as:\n1. **Assessment** (2-3 sentences: overall risk level and why)\n2. **Key Risk Factors** (bullet points of the most concerning signals)\n3. **Pattern Analysis** (what do the events suggest about this vessel's behavior?)\n4. **Recommended Actions** (specific next steps for investigators)\n5. **Information Gaps** (what additional data would strengthen the assessment?)",
            }
        ],
    )

    brief_text = response.content[0].text

    result = {
        "mmsi": mmsi,
        "brief": brief_text,
        "vessel_name": ctx["vessel"]["name"],
        "risk_score": ctx["vessel"]["risk_score"],
        "alert_tier": ctx["vessel"]["alert_tier"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    _brief_cache[mmsi] = {"data": result, "generated_at": datetime.now(timezone.utc)}
    return result


class ChatRequest(BaseModel):
    mmsi: str
    messages: list[dict]


@router.post("/ai/chat")
async def ai_chat(req: ChatRequest, db: AsyncSession = Depends(get_db)):
    """Streaming conversational AI chat about a vessel."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    ctx = await _gather_vessel_context(req.mmsi, db)
    context_msg = _build_vessel_context_message(ctx)

    # Build messages: inject vessel context as first user message
    messages = [
        {"role": "user", "content": f"I'm analyzing a vessel. Here is all available data:\n\n{context_msg}"},
        {"role": "assistant", "content": "I've reviewed the vessel data. What would you like to know?"},
    ]
    # Append conversation history
    for m in req.messages:
        messages.append({"role": m["role"], "content": m["content"]})

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    async def event_stream():
        with client.messages.stream(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
