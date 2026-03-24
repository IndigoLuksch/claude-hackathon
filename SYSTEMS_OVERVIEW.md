# DarkFleet — Systems Overview

> Planning document. Describes the target architecture, data sources, database schema,
> scoring design, API surface, and frontend. Covers both what currently exists and
> what must be built.

---

## 1. Architecture

```
External data sources
─────────────────────────────────────────────────────────────────────────
  Global Fishing Watch API  →  scripts/gfw_ingest.py          (vessels + events)
  IMO GISIS                 →  scripts/imo_gisis_enrich.py     (ownership/registry)
  FAO HSVAR                 →  scripts/fao_hsvar_ingest.py     (high-seas authorization)
  CCAMLR IUU list (PDF)     →  scripts/ccamlr_ingest.py        (blacklist)
  WCPFC IUU list (Excel)    →  scripts/wcpfc_ingest.py         (blacklist)
  Paris MOU / THETIS        →  scripts/paris_mou_ingest.py     (detention records)
  OpenSanctions (JSON)      →  loaded at startup in scoring.py (sanctions)

Application layer
─────────────────────────────────────────────────────────────────────────
  FastAPI (app/)
    main.py                 — lifespan, router registration, static file mount
    database.py             — async engine, session factory, Base
    models.py               — SQLAlchemy ORM models
    scoring.py              — signal functions, score_and_persist(), build_signal_details()
    routers/
      ingest.py             — POST /ingest, GET /ingest/status
      vessels.py            — GET /vessels, /vessel-trails, /vessels/{mmsi}/events
      scoring.py            — POST /score/all, /score/{mmsi}, GET /alerts
      reports.py            — GET /report/{mmsi}, /report/{mmsi}/html, /vessels/{mmsi}/signals
      mpa.py                — GET /mpa

Frontend
─────────────────────────────────────────────────────────────────────────
  frontend/index.html       — single-file SPA, Mapbox GL JS v3.3.0

Storage
─────────────────────────────────────────────────────────────────────────
  PostgreSQL + PostGIS
```

---

## 2. Data Sources

### 2.1  Global Fishing Watch (existing)

- **API**: `https://gateway.api.globalfishingwatch.org/v3`
- **Auth**: `Bearer $GFW_API_KEY`
- **Vessels**: `GET /v3/vessels/search` — dataset `public-global-vessel-identity:latest`, paginated 50/page via `since` cursor, exhausted per vessel type.
- **Events**: `GET /v3/events` — three datasets in parallel per request:
  - `public-global-fishing-events:latest`
  - `public-global-gaps-events:latest`
  - `public-global-loitering-events:latest`
- **Limitation**: events endpoint returns max 200 per request with no pagination token — this silently truncates vessels with long histories. **Fix needed**: paginate using `offset` until `entries` count < `limit`.
- **Vessel types fetched**: 15 (fishing gear types + CARRIER, BUNKER, SUPPORT_MOTHER_SHIP).

### 2.2  IMO GISIS — Ownership/Registry Verification

- **URL**: `https://gisis.imo.org/`
- **Reality**: The entire public module requires a free IMO account. The ship search redirects to `webaccounts.imo.org/Login.aspx`. There is **no unauthenticated REST API**.
- **Practical approach**:
  1. Register at `webaccounts.imo.org` (free).
  2. Store credentials in `.env` as `IMO_GISIS_USERNAME` / `IMO_GISIS_PASSWORD`.
  3. `scripts/imo_gisis_enrich.py` authenticates via POST to `webaccounts.imo.org/Login.aspx`, then queries `gisis.imo.org/Public/SHIPS/Default.aspx?imo={imo}` per vessel, parses the returned HTML (BeautifulSoup) to extract owner, manager, flag, and status.
  4. Rate-limit to 1 request/second. Run as a post-ingest enrichment, not in the hot path.
- **Data extracted**: registered owner name + country, ship manager, technical manager, flag state, vessel status (active/scrapped/etc.).
- **Alternative**: LR Fairplay at `imonumbers.lrfairplay.com` (free, issues IMO numbers on behalf of IMO) for basic IMO number verification, though it does not provide ownership chains.
- **New table**: `vessel_ownership` (see §3).

### 2.3  FAO HSVAR — High Seas Vessel Authorization Record

- **What it is**: The FAO Global Record tracks vessels authorized by their flag states to fish on the high seas. Absence from this record for a vessel operating in high-seas zones is a direct IUU indicator.
- **Access**: FAO FishFinder web service at `https://www.fao.org/fishery/en/vessel/search`. The FAO does publish machine-readable data for some collections via their REST APIs at `https://www.fao.org/fishery/api/`. Investigate `GET /fishery/api/collection/globalRecord` for bulk CSV/JSON exports.
- **Fallback**: Scrape the search interface with HTTPX + BeautifulSoup, querying by flag state in batches.
- **Ingest script**: `scripts/fao_hsvar_ingest.py` — fetches all currently authorized vessels and upserts into `fao_hsvar_authorizations` table.
- **Run frequency**: Monthly (list is updated periodically by flag states).
- **New table**: `fao_hsvar_authorizations` (see §3).

### 2.4  CCAMLR IUU Vessel Blacklist

- **URL**: `https://www.ccamlr.org/en/compliance/iuu-vessel-lists`
- **Format**: PDF (~45 KB). Contains vessel name, IMO number, call sign, flag state, listing date, activities, and ownership history per vessel.
- **Ingest script**: `scripts/ccamlr_ingest.py` — downloads the PDF, parses with `pdfplumber`, extracts rows, upserts into `iuu_blacklist` with `source='CCAMLR'`.
- **Run frequency**: Annually (CCAMLR publishes after its November annual meeting), or on-demand.
- **Hard trigger in scoring**: any vessel matched against this list → `alert_tier = "red"` regardless of numeric score.

### 2.5  WCPFC IUU Vessel Blacklist

- **URL**: `https://www.wcpfc.int/` — IUU vessel list published as Excel or web table.
- **Format**: Excel (.xlsx) or HTML table depending on year.
- **Ingest script**: `scripts/wcpfc_ingest.py` — downloads and parses using `openpyxl` or BeautifulSoup, upserts with `source='WCPFC'`.
- **Run frequency**: Annually (WCPFC publishes after its December annual session).
- **Hard trigger**: same as CCAMLR — any match forces red tier.

### 2.6  Paris MOU Detention Records

- **URL**: `https://www.parismou.org/`
- **What it is**: Port State Control inspection and detention records. A detained vessel has been physically stopped by port authorities for safety or compliance violations — the strongest possible hard signal short of criminal conviction.
- **Access**: The Paris MOU operates THETIS via EMSA (`thetis.emsa.europa.eu`). The public web interface has a ship search but no documented bulk API. Options:
  1. Session-based scraping of `https://www.parismou.org/inspection-search` with rate limiting.
  2. If THETIS EMSA provides XML/JSON exports for accredited users, prefer that.
- **Ingest script**: `scripts/paris_mou_ingest.py`
- **New table**: `detention_records` (see §3).
- **Hard trigger**: any vessel with a detention record in the last 24 months → minimum `alert_tier = "amber"`.

### 2.7  OpenSanctions (existing, partial)

- **Format**: Local JSON file at `$OPENSANCTIONS_PATH` (default: `opensanctions.json`).
- **Currently used**: flag state codes and vessel names matched at startup.
- **Gap**: OpenSanctions also contains IMO numbers and MMSI. The current match is name/flag only — should add IMO and MMSI lookups.
- **Download**: `https://data.opensanctions.org/datasets/latest/vessels/targets.json` (free for non-commercial).

---

## 3. Database Schema

### 3.1  Existing Tables

#### `vessels`
```
mmsi              TEXT PK
imo               TEXT
name              TEXT
flag_state        TEXT
gear_type         TEXT
last_seen         TIMESTAMPTZ
risk_score        FLOAT  DEFAULT 0
alert_tier        TEXT   DEFAULT 'clear'
flag_history_json JSONB
```

#### `events`
```
id            TEXT PK   (GFW event ID or UUID)
vessel_mmsi   TEXT  INDEX
event_type    TEXT       (FISHING | GAP | LOITERING | ENCOUNTER | TRANSSHIPMENT)
timestamp     TIMESTAMPTZ
lat           FLOAT
lon           FLOAT
details_json  JSONB
```
**Known gap**: `fetch_events()` sends `limit=200, offset=0` with no loop. Vessels with > 200 events in the window silently lose data. Fix: paginate with `offset += 200` until returned count < 200.

#### `mpa_zones`
```
id        SERIAL PK
name      TEXT
geometry  GEOMETRY(GEOMETRY, 4326)  -- Polygon or MultiPolygon
```
Contains 50 zones (29 FAO areas + 21 high-seas pockets) from Marine Regions WFS.

#### `rfmo_authorised`
```
id                SERIAL PK
mmsi              TEXT  INDEX
imo               TEXT
rfmo_name         TEXT
authorised_species TEXT
authorised_zone    TEXT
```
**Currently empty** — this is why RFMO absence grants every vessel 10 pts. Scoring must treat an empty table as "no signal, no points" (already partially fixed: returns 10 pts for no_data vs 20 pts for absent; target is 0 pts for no_data).

---

### 3.2  New Tables

#### `vessel_ownership`
Populated by `scripts/imo_gisis_enrich.py`.
```
id                   SERIAL PK
mmsi                 TEXT  INDEX (soft ref to vessels.mmsi)
imo                  TEXT  INDEX
registered_owner     TEXT
registered_owner_country TEXT
ship_manager         TEXT
technical_manager    TEXT
flag_state           TEXT
vessel_status        TEXT   -- active | scrapped | total_loss | etc.
source               TEXT   DEFAULT 'GISIS'
verified_at          TIMESTAMPTZ
```

#### `iuu_blacklist`
Populated by `scripts/ccamlr_ingest.py` and `scripts/wcpfc_ingest.py`.
```
id            SERIAL PK
source        TEXT        -- CCAMLR | WCPFC | SEAFO | etc.
vessel_name   TEXT
imo           TEXT  INDEX
mmsi          TEXT  INDEX
flag_state    TEXT
call_sign     TEXT
listing_date  DATE
reason        TEXT
raw_text      TEXT
```
Unique constraint: `(source, imo)` — prevents duplicate entries per source per vessel.

#### `fao_hsvar_authorizations`
Populated by `scripts/fao_hsvar_ingest.py`.
```
id                SERIAL PK
imo               TEXT  INDEX
vessel_name       TEXT
flag_state        TEXT
authorized_by     TEXT   -- flag state ISO-2
authorization_type TEXT
fishing_areas     TEXT[] -- JSONB array of FAO area codes
valid_from        DATE
valid_until       DATE
source_updated    DATE
```

#### `detention_records`
Populated by `scripts/paris_mou_ingest.py`.
```
id                SERIAL PK
imo               TEXT  INDEX
mmsi              TEXT  INDEX
vessel_name       TEXT
inspection_date   DATE
port              TEXT
flag_state        TEXT
deficiency_count  INT
detained          BOOLEAN
authority         TEXT   -- paris_mou | tokyo_mou | uscg | etc.
raw_json          JSONB
```

---

## 4. Scoring Engine

### 4.1  Design Principles

1. **All signals use a 12-month rolling window** unless the signal is structural (blacklist, RFMO status).
2. **IUU blacklist is a hard trigger**: any match forces `alert_tier = "red"` regardless of numeric score.
3. **Detention is a hard floor**: a detained vessel (last 24 months) is never `"clear"`.
4. **Empty reference tables contribute 0 points** — "no data" is not evidence of guilt.
5. **Geographic context is reinstated**: gaps and loitering near or inside MPAs score higher. The PostGIS infrastructure already exists in `mpa_zones`.
6. **Transshipment is the strongest behavioral signal** — it is the primary mechanism for laundering illegally caught fish at sea.
7. **Peer baseline comparison** mitigates false positives from gear types that structurally generate more gaps (e.g. squid jig vessels that shut off AIS in transit).

### 4.2  Signal Table

| Signal | Max pts | Condition | Notes |
|---|---|---|---|
| **IUU blacklist match** | hard → red | Any match in `iuu_blacklist` | CCAMLR or WCPFC. Forces `alert_tier="red"`, does not add to numeric score |
| **Transshipment** | 30 | 10 pts/event in 12 months, cap 3 | Was 5 pts/90d. Strongest behavioral IUU signal |
| **AIS dark gap — near MPA** | 25 | 12.5 pts/gap >6 h within 50 km of MPA polygon | PostGIS `ST_DWithin` against `mpa_zones` |
| **Detention record** | 20 | 20 pts if detained in last 24 months | Paris/Tokyo MOU `detention_records.detained=true` |
| **RFMO absent** | 15 | Only when `rfmo_authorised` table is non-empty | 0 if table empty |
| **Loitering — inside MPA** | 15 | 7.5 pts/event >2 h with position `ST_Within` MPA | PostGIS |
| **AIS dark gap — open ocean** | 10 | 5 pts/gap >6 h, not near MPA, cap 2 | Separate from MPA-proximity gaps |
| **Peer anomaly** | 10 | Vessel's 12-month gap count > (peer_mean + 1.5 × peer_stddev) for same gear + flag | SQL window function or subquery |
| **Loitering — open ocean** | 6 | 3 pts/event >2 h, not inside MPA, cap 2 | |
| **Flag changes** | 8 | 4 pts per additional flag in 12 months, cap 2 changes | |
| **Ownership opacity** | 5 | Flag-of-convenience registry + no `vessel_ownership` record | Heuristic |
| **Sanctions match** | 5 | Name, IMO, or MMSI in OpenSanctions | |

**Total theoretical max**: ~149 pts → clamped to 100.
**Tier thresholds**: red ≥ 80 · amber ≥ 50 · clear < 50

Note: amber threshold lowered from 60 to 50 to surface vessels with multiple moderate signals.

### 4.3  Peer Baseline SQL

```sql
-- Compute peer average gap count for same gear_type and flag_state
WITH peer_gaps AS (
    SELECT e.vessel_mmsi, COUNT(*) AS gap_count
    FROM   events e
    JOIN   vessels v ON v.mmsi = e.vessel_mmsi
    WHERE  e.event_type = 'GAP'
      AND  e.timestamp >= NOW() - INTERVAL '12 months'
      AND  v.gear_type  = :gear_type
      AND  v.flag_state = :flag_state
    GROUP  BY e.vessel_mmsi
)
SELECT
    COALESCE(AVG(gap_count), 0)    AS peer_mean,
    COALESCE(STDDEV(gap_count), 0) AS peer_stddev,
    COUNT(*)                        AS peer_n
FROM peer_gaps
```

Apply signal only when `peer_n >= 5` (insufficient peer sample otherwise).

### 4.4  Geographic Signal SQL (PostGIS)

```sql
-- Gaps within 50 km of any MPA polygon
SELECT COUNT(*) FROM events e
WHERE e.vessel_mmsi = :mmsi
  AND e.event_type  = 'GAP'
  AND e.timestamp  >= NOW() - INTERVAL '12 months'
  AND COALESCE((e.details_json->'gap'->>'durationHours')::float, 0) > 6
  AND EXISTS (
      SELECT 1 FROM mpa_zones m
      WHERE ST_DWithin(
          m.geometry::geography,
          ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326)::geography,
          50000   -- metres
      )
  )

-- Loitering events whose position falls inside an MPA polygon
SELECT COUNT(*) FROM events e
WHERE e.vessel_mmsi = :mmsi
  AND e.event_type  = 'LOITERING'
  AND e.timestamp  >= NOW() - INTERVAL '12 months'
  AND COALESCE((e.details_json->'loitering'->>'totalTimeHours')::float, 0) > 2
  AND EXISTS (
      SELECT 1 FROM mpa_zones m
      WHERE ST_Within(
          ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326),
          m.geometry
      )
  )
```

### 4.5  `score_and_persist()` Changes

- Remove the `no_data → 10 pts` RFMO branch. Return `0.0, "no_data"` instead.
- After computing numeric score and tier, run a blacklist lookup:
  ```sql
  SELECT EXISTS (
      SELECT 1 FROM iuu_blacklist
      WHERE imo = :imo OR mmsi = :mmsi
  )
  ```
  If true → force `alert_tier = "red"`, set `iuu_blacklisted = true` on vessel row.
- Run detention lookup:
  ```sql
  SELECT EXISTS (
      SELECT 1 FROM detention_records
      WHERE (imo = :imo OR mmsi = :mmsi)
        AND detained = true
        AND inspection_date >= NOW() - INTERVAL '24 months'
  )
  ```
  If true → floor tier to `"amber"` minimum.

### 4.6  New `Vessel` Columns Required

```
iuu_blacklisted   BOOLEAN  DEFAULT false
blacklist_source  TEXT     -- CCAMLR | WCPFC | null
detained_24m      BOOLEAN  DEFAULT false
ownership_verified BOOLEAN DEFAULT false
```

---

## 5. Ingestion Scripts

### 5.1  `scripts/gfw_ingest.py` (fix events pagination)

Current `fetch_events()` sends a single request with `limit=200, offset=0`. Replace with:

```python
async def fetch_events(client, gfw_vessel_ids, start_date, end_date):
    all_events = []
    for i in range(0, len(gfw_vessel_ids), BATCH_SIZE):  # BATCH_SIZE = 10
        batch = gfw_vessel_ids[i:i+BATCH_SIZE]
        offset = 0
        while True:
            params = build_params(batch, start_date, end_date, limit=200, offset=offset)
            r = await client.get(f"{GFW_BASE}/events", params=params, headers=_headers())
            r.raise_for_status()
            entries = r.json().get("entries", [])
            all_events.extend(entries)
            if len(entries) < 200:
                break
            offset += 200
    return all_events
```

### 5.2  `scripts/imo_gisis_enrich.py` (new)

```
Inputs:  All vessels in DB with a non-null IMO number not yet verified
         (WHERE ownership_verified = false AND imo IS NOT NULL)
Process: 1. POST to webaccounts.imo.org/Login.aspx with GISIS credentials
         2. Extract session cookie
         3. For each IMO: GET gisis.imo.org/Public/SHIPS/Default.aspx?imo={imo}
         4. Parse HTML with BeautifulSoup → extract owner, manager, status
         5. Upsert into vessel_ownership
         6. Set vessels.ownership_verified = true
Rate:    1 req/sec, max 500 vessels per run
Env:     IMO_GISIS_USERNAME, IMO_GISIS_PASSWORD
Deps:    httpx, beautifulsoup4, lxml
```

### 5.3  `scripts/fao_hsvar_ingest.py` (new)

```
Inputs:  FAO Global Record API or bulk CSV
Process: 1. GET https://www.fao.org/fishery/api/collection/globalRecord (explore endpoint)
            Fallback: scrape https://www.fao.org/fishery/en/vessel/search by flag state
         2. Parse response (JSON or HTML table)
         3. Extract: IMO, vessel name, flag, authorization status, fishing areas, validity
         4. Truncate + re-insert fao_hsvar_authorizations (full refresh)
Deps:    httpx, beautifulsoup4 (fallback scraper)
```

### 5.4  `scripts/ccamlr_ingest.py` (new)

```
Inputs:  PDF from https://www.ccamlr.org/en/compliance/iuu-vessel-lists
Process: 1. Download PDF
         2. Parse with pdfplumber — extract table rows
         3. Parse vessel name, IMO, flag, call sign, listing date, activities
         4. Upsert into iuu_blacklist WHERE source='CCAMLR'
         5. Force iuu_blacklisted=true on any matched vessels row
Deps:    httpx, pdfplumber
```

### 5.5  `scripts/wcpfc_ingest.py` (new)

```
Inputs:  Excel/HTML from https://www.wcpfc.int/
Process: 1. Locate IUU vessel list link (check annually, URL may change)
         2. Download and parse with openpyxl (xlsx) or BeautifulSoup (HTML table)
         3. Upsert into iuu_blacklist WHERE source='WCPFC'
Deps:    httpx, openpyxl, beautifulsoup4
```

### 5.6  `scripts/paris_mou_ingest.py` (new)

```
Inputs:  Paris MOU inspection search (session-based scraping)
Process: 1. GET https://www.parismou.org/inspection-search with filters
            (detained=true, date range: last 24 months)
         2. Paginate through results
         3. Extract: IMO, vessel name, port, date, deficiency count, detained flag
         4. Upsert into detention_records WHERE authority='paris_mou'
         5. Update vessels.detained_24m flag
Deps:    httpx, beautifulsoup4
Note:    If THETIS (EMSA) provides structured access, prefer that over scraping
```

---

## 6. API Layer

### 6.1  Existing Routes (no changes)

| Method | Path | Description |
|---|---|---|
| `GET` | `/vessels` | Paginated vessel list, ordered by risk_score DESC |
| `GET` | `/vessels/{mmsi}/events` | Last N events for a vessel |
| `GET` | `/vessel-trails` | GeoJSON FeatureCollection of vessel trail LineStrings |
| `GET` | `/mpa` | GeoJSON of MPA/FAO zones |
| `GET` | `/alerts` | All vessels + most recent event, ordered by risk_score |
| `POST` | `/score/{mmsi}` | Rescore single vessel |
| `POST` | `/score/all` | Rescore all vessels |
| `GET` | `/report/{mmsi}` | PDF incident report (WeasyPrint) |
| `GET` | `/report/{mmsi}/html` | HTML preview of report |

### 6.2  New Routes Needed

| Method | Path | Description |
|---|---|---|
| `GET` | `/vessels/{mmsi}/ownership` | Registry and ownership details from `vessel_ownership` |
| `GET` | `/vessels/{mmsi}/blacklist` | IUU blacklist entries for this vessel |
| `GET` | `/vessels/{mmsi}/detentions` | Detention records for this vessel |
| `POST` | `/enrich/{mmsi}` | Trigger GISIS enrichment for one vessel |
| `POST` | `/enrich/all` | Trigger GISIS enrichment for all un-verified vessels |
| `GET` | `/iuu-blacklist` | Full blacklist summary (source, count, last updated) |

### 6.3  Modified Routes

**`POST /ingest`**: After GFW ingest, chain enrichment steps as optional flags:
```
POST /ingest?enrich_ownership=true&reload_blacklists=true
```

**`GET /vessels/{mmsi}/signals`**: Add new signals to response — peer anomaly, blacklist status, detention status, GISIS ownership verification status.

---

## 7. Frontend

The frontend is a single HTML file (`frontend/index.html`). Changes should stay within this file.

### 7.1  Left Panel — Vessel Cards

- Add a **red skull icon** (⚠) on cards for IUU-blacklisted vessels.
- Add a **lock icon** for vessels with unverified ownership.
- Tier badge color already driven by `alert_tier` — blacklist forces red so this propagates automatically.

### 7.2  Right Panel — Vessel Detail

**Ownership section** (new, between metadata grid and score ring):
- Registered owner, country, ship manager (from `vessel_ownership`)
- "Ownership verified via GISIS" badge, or "Unverified" warning
- Trigger enrich button if unverified

**IUU Blacklist section** (new, shown only if vessel is blacklisted):
- Source (CCAMLR / WCPFC), listing date, activities stated in the listing
- Hard red visual treatment

**Detention records** (new, shown only if detention record exists):
- Most recent detention: port, date, deficiency count
- "Detained within 24 months" warning badge

**Risk Signals** (existing, keep — expand with new signals):
- Peer anomaly: "Gap rate 3.1× fleet average (gear: TRAWLER, flag: KR)"
- Transshipment now shown first (highest weight signal)
- Geographic context shown in detail: "2 gaps within 50 km of MPA"

### 7.3  Map Layers

- **No new layers needed** — blacklisted vessels will already render red (forced tier).
- Consider adding a map popup on vessel click that shows blacklist status inline, before the user opens the full details panel.

### 7.4  Reload Flow

Current: single `POST /ingest`. Target: two-stage with progress:
```
POST /ingest          → triggers GFW fetch, returns when complete
POST /enrich/all      → triggers GISIS enrichment for new vessels
```
Show separate status for each stage in the subtitle bar.

---

## 8. Configuration (`.env`)

```bash
# Existing
DATABASE_URL=postgresql+asyncpg://darkfleet:darkfleet@localhost:5432/darkfleet
GFW_API_KEY=...
OPENSANCTIONS_PATH=opensanctions.json

# New
IMO_GISIS_USERNAME=...       # free IMO account for GISIS access
IMO_GISIS_PASSWORD=...
```

---

## 9. Dependencies (additions to `requirements.txt`)

```
pdfplumber        # CCAMLR PDF parsing
openpyxl          # WCPFC Excel parsing
beautifulsoup4    # HTML scraping (GISIS, Paris MOU, FAO fallback)
lxml              # BeautifulSoup parser backend
```

Existing: `fastapi`, `sqlalchemy[asyncio]`, `asyncpg`, `geoalchemy2`, `httpx`, `weasyprint`, `jinja2`, `python-dotenv`.

---

## 10. Implementation Order

The following order minimises broken states at each step:

1. **Fix GFW event pagination** — immediately improves data quality for scoring, no schema changes.
2. **Add new DB columns to `vessels`** (`iuu_blacklisted`, `blacklist_source`, `detained_24m`, `ownership_verified`) and create new tables (`vessel_ownership`, `iuu_blacklist`, `detention_records`, `fao_hsvar_authorizations`).
3. **CCAMLR ingest** — highest-value blacklist, PDF is already confirmed downloadable. Immediately enables hard-trigger signals for known IUU vessels.
4. **WCPFC ingest** — second blacklist, Excel format.
5. **Scoring redesign** — update `scoring.py` with new signal table, geographic PostGIS queries, peer baseline, blacklist hard trigger, detention floor, RFMO empty-table fix.
6. **Paris MOU ingest** — detention records, provides the detained_24m signal.
7. **FAO HSVAR ingest** — authorization records, provides an independent verification layer beyond RFMO.
8. **IMO GISIS enrichment** — requires account registration. Runs as post-ingest enrichment, not blocking. Adds ownership/registry depth to reports.
9. **API new routes** — ownership, blacklist, detention endpoints.
10. **Frontend updates** — blacklist indicators, ownership panel, detention records, updated signal display.

---

## 11. Known Gaps / Open Questions

| Item | Status | Notes |
|---|---|---|
| GFW event pagination | Bug in current code | events silently capped at 200 per vessel |
| RFMO authorised table | Empty | RFMO CSV data needs to be sourced and loaded |
| CCAMLR list format | PDF confirmed | Needs `pdfplumber` integration |
| WCPFC list URL/format | 403 during research | Investigate alternate URL or annual report pages |
| Paris MOU bulk access | No API found | Session scraping or THETIS EMSA route |
| FAO HSVAR API | Endpoint TBC | Investigate `fao.org/fishery/api/` first |
| IMO GISIS | No public API | Free account required, session-based access |
| OpenSanctions IMO/MMSI matching | Missing | Currently only name + flag matched |
| Encounters dataset | 404 for this API key | `public-global-encounter-events:latest` unavailable — verify with GFW |
