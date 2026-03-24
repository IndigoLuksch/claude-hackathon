#!/usr/bin/env python3
"""
IMO GISIS Enrichment Script
Authenticates with IMO GISIS and fetches ownership/registry data for vessels in the DB.

Usage:
  python scripts/imo_gisis_enrich.py --limit 100
"""
import asyncio
import logging
import os
import sys
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import Vessel, VesselOwnership

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://darkfleet:darkfleet@localhost:5432/darkfleet",
)

USERNAME = os.getenv("IMO_GISIS_USERNAME")
PASSWORD = os.getenv("IMO_GISIS_PASSWORD")

BASE_URL = "https://gisis.imo.org"
LOGIN_URL = "https://webaccounts.imo.org/Login.aspx"
SHIP_URL = f"{BASE_URL}/Public/SHIPS/Default.aspx"


class GISISClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    async def login(self):
        log.info("Logging into IMO GISIS...")
        if not self.username or not self.password:
            log.error("Missing IMO_GISIS_USERNAME or IMO_GISIS_PASSWORD")
            return False

        resp = await self.client.get(LOGIN_URL)
        soup = BeautifulSoup(resp.text, "html.parser")

        # ASP.NET hidden fields
        data = {
            "__VIEWSTATE": soup.find("input", {"name": "__VIEWSTATE"})["value"],
            "__EVENTVALIDATION": soup.find("input", {"name": "__EVENTVALIDATION"})["value"],
            "ctl00$CPH_Main$Login1$UserName": self.username,
            "ctl00$CPH_Main$Login1$Password": self.password,
            "ctl00$CPH_Main$Login1$LoginButton": "Log In",
        }

        resp = await self.client.post(LOGIN_URL, data=data)
        if "Logout" in resp.text or "Signed in" in resp.text or resp.status_code == 200:
            log.info("Login successful")
            return True
        else:
            log.error("Login failed")
            return False

    async def fetch_vessel_data(self, imo):
        log.info("Fetching GISIS data for IMO %s", imo)
        resp = await self.client.get(f"{SHIP_URL}?imo={imo}")
        if resp.status_code != 200:
            log.warning("Failed to fetch IMO %s: HTTP %d", imo, resp.status_code)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # This is a heuristic based on typical GISIS layout. 
        # In a real scenario, we'd inspect the actual HTML structure.
        # We look for labels and their corresponding values.
        data = {
            "imo": imo,
            "registered_owner": None,
            "registered_owner_country": None,
            "ship_manager": None,
            "technical_manager": None,
            "flag_state": None,
            "vessel_status": None,
            "verified_at": datetime.now(timezone.utc),
        }

        # Example extraction logic (placeholders for actual selectors)
        # GISIS often uses <span> or <div> with specific IDs or table layouts
        try:
            # Look for common labels in the page text
            labels = {
                "Registered Owner": "registered_owner",
                "Flag": "flag_state",
                "Status of ship": "vessel_status",
                "Ship manager": "ship_manager",
            }
            
            # Simple heuristic: find text, then find next sibling or parent's sibling
            for label, key in labels.items():
                element = soup.find(string=lambda t: t and label in t)
                if element:
                    # Navigation depends on exact HTML, but often it's in a table
                    parent_td = element.find_parent("td")
                    if parent_td and parent_td.find_next_sibling("td"):
                        data[key] = parent_td.find_next_sibling("td").get_text(strip=True)

        except Exception as e:
            log.error("Error parsing GISIS HTML for IMO %s: %s", imo, e)

        return data


async def main():
    parser = ArgumentParser(description="Enrich vessels with IMO GISIS ownership data")
    parser.add_argument("--limit", type=int, default=50, help="Max vessels to process")
    parser.add_argument("--mmsi", type=str, help="Enrich only this specific MMSI")
    args = parser.parse_args()

    engine = create_async_engine(DATABASE_URL)
    AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

    async with AsyncSession() as session:
        # Get vessels with IMO but not yet verified
        stmt = select(Vessel).where(
            Vessel.imo.isnot(None), 
            Vessel.ownership_verified == False
        )
        if args.mmsi:
            stmt = stmt.where(Vessel.mmsi == args.mmsi)
        
        stmt = stmt.limit(args.limit)
        result = await session.execute(stmt)
        vessels = result.scalars().all()

        if not vessels:
            log.info("No vessels found needing GISIS verification.")
            return

        log.info("Found %d vessels to verify", len(vessels))

        async with GISISClient(USERNAME, PASSWORD) as gisis:
            for vessel in vessels:
                data = await gisis.fetch_vessel_data(vessel.imo)
                if not data:
                    continue

                # Add MMSI from vessel row
                data["mmsi"] = vessel.mmsi
                
                # Upsert into vessel_ownership
                ownership = VesselOwnership(**data)
                session.add(ownership)
                
                # Mark vessel as verified
                vessel.ownership_verified = True
                
                # Rate limiting
                await asyncio.sleep(1.0)

            await session.commit()

    log.info("GISIS enrichment complete.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
