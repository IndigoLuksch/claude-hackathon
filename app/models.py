import uuid
from datetime import datetime, timezone
from typing import Optional

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Vessel(Base):
    __tablename__ = "vessels"

    mmsi: Mapped[str] = mapped_column(String(20), primary_key=True)
    imo: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    flag_state: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    gear_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    alert_tier: Mapped[str] = mapped_column(
        String(10), default="clear", server_default="'clear'", nullable=False
    )
    # List of {"flag": "XX", "first_transmission": "...", "last_transmission": "..."}
    # populated by gfw_ingest.py from selfReportedInfo identities.
    flag_history_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # New columns for enrichment
    iuu_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    blacklist_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    detained_24m: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ownership_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        Index("ix_vessels_risk_score", "risk_score"),
        Index("ix_vessels_alert_tier", "alert_tier"),
    )


class VesselOwnership(Base):
    __tablename__ = "vessel_ownership"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mmsi: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    imo: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    registered_owner: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    registered_owner_country: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ship_manager: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    technical_manager: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    flag_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    vessel_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="GISIS", server_default="'GISIS'")
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Event(Base):
    __tablename__ = "events"

    # Use GFW's own event ID when available; fall back to a fresh UUID.
    id: Mapped[str] = mapped_column(
        String(128), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    vessel_mmsi: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    details_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)



class MPAZone(Base):
    __tablename__ = "mpa_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(500))
    # Accept Polygon and MultiPolygon from WDPA data.
    geometry = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=False)


class RFMOAuthorised(Base):
    __tablename__ = "rfmo_authorised"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mmsi: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    imo: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    rfmo_name: Mapped[str] = mapped_column(String(100))
    authorised_species: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    authorised_zone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
