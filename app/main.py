from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routers import jobs, pipeline, stories
from app.setup import ensure_assets
from app.tasks.pipeline_tasks import pipeline_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Auto-download font and Voxtral GGUF model if not already present.
    # Slow on first run (~30s for font, several minutes for model download),
    # so it runs in a thread instead of blocking the event loop.
    await asyncio.to_thread(ensure_assets)
    init_db()
    # Launch the single serialised pipeline worker — ensures only one
    # TTS/WhisperX/ffmpeg job runs at a time (prevents OOM).
    worker = asyncio.create_task(pipeline_worker())
    logger.info("Startup complete — assets ready, database initialised.")
    yield
    worker.cancel()


app = FastAPI(
    title="Infinity Mystery Video Factory",
    description="Automated horror story → vertical video pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

# The Streamlit UI calls the API server-side; browsers only need it for
# local tools such as the Swagger UI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stories.router)
app.include_router(jobs.router)
app.include_router(pipeline.router)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok"}
