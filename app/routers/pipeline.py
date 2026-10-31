from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Story
from app.schemas import PipelineRunRequest, PipelineRunResponse
from app.tasks.pipeline_tasks import enqueue_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/run", response_model=PipelineRunResponse)
async def trigger_pipeline(
    payload: PipelineRunRequest,
    db: Session = Depends(get_db),
) -> PipelineRunResponse:
    story = db.get(Story, payload.story_id)
    if story is None:
        raise HTTPException(status_code=404, detail=f"Story {payload.story_id} not found")

    # Go through the serialised queue — running the pipeline as a separate
    # BackgroundTask would bypass the one-job-at-a-time RAM/VRAM guard.
    added = await enqueue_pipeline(payload.story_id, payload.language_code)

    logger.info(
        "Pipeline triggered for story %d, lang=%s", payload.story_id, payload.language_code
    )
    return PipelineRunResponse(
        story_id=payload.story_id,
        language_code=payload.language_code,
        message="Pipeline enqueued." if added else "Pipeline already queued.",
    )
