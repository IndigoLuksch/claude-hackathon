import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import ingest
from app.routers import mpa
from app.routers import vessels
from app.routers import scoring
from app.routers import reports
from app.routers import enrich


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="DarkFleet",
    description="Maritime Surveillance System",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/config")
async def get_config():
    return {"mapbox_token": os.getenv("MAPBOX_TOKEN", "")}


app.include_router(ingest.router)
app.include_router(vessels.router)
app.include_router(scoring.router)
app.include_router(reports.router)
app.include_router(mpa.router)
app.include_router(enrich.router)
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
