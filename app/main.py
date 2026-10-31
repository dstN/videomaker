from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import asyncio

from app.database import init_db
from app.routers import jobs, pipeline, stories
from app.setup import ensure_assets
from app.tasks.pipeline_tasks import pipeline_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Infinity Mystery Video Factory",
    description="Automated horror story → vertical video pipeline",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stories.router)
app.include_router(jobs.router)
app.include_router(pipeline.router)


@app.on_event("startup")
async def startup() -> None:
    # Auto-download font and Voxtral GGUF model if not already present.
    # Blocking on first run (~30s for font, several minutes for model download).
    # Subsequent startups return immediately if assets are present.
    ensure_assets()
    init_db()
    # Launch the single serialised pipeline worker — ensures only one
    # TTS/WhisperX/ffmpeg job runs at a time (prevents OOM).
    asyncio.create_task(pipeline_worker())
    logging.getLogger(__name__).info("Startup complete — assets ready, database initialised.")


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok"}
