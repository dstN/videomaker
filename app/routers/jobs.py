from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RenderJob, VideoPart, Translation, Story
from app.schemas import RenderJobList, RenderJobResponse
from app.tasks.pipeline_tasks import enqueue_retry, retry_all_stories

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/", response_model=RenderJobList)
def list_jobs(db: Session = Depends(get_db)) -> RenderJobList:
    rows = (
        db.query(RenderJob, VideoPart, Translation, Story)
        .join(VideoPart, VideoPart.id == RenderJob.video_part_id)
        .join(Translation, Translation.id == VideoPart.translation_id)
        .join(Story, Story.id == Translation.story_id)
        .order_by(RenderJob.updated_at.desc())
        .all()
    )
    result = []
    for job, part, translation, story in rows:
        item = RenderJobResponse(
            id=job.id,
            video_part_id=job.video_part_id,
            story_id=story.id,
            story_title=story.title,
            part_number=part.part_number,
            status=job.status,
            file_path=job.file_path,
            error_message=job.error_message,
            updated_at=job.updated_at,
        )
        result.append(item)
    return RenderJobList(jobs=result, total=len(result))


@router.get("/{job_id}", response_model=RenderJobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)) -> RenderJob:
    job = db.get(RenderJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"RenderJob {job_id} not found")
    return job


@router.post("/{job_id}/retry", response_model=RenderJobResponse)
async def retry_render_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> RenderJob:
    job = db.get(RenderJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"RenderJob {job_id} not found")

    added = await enqueue_retry(job_id)
    if added:
        logger.info("Retry queued for job %d", job_id)
    else:
        logger.info("Retry for job %d already in queue — skipped", job_id)
    return job


@router.post("/retry-all")
async def retry_all_jobs() -> dict:
    """Reset all artefacts and re-queue every story for a full pipeline run."""
    queued = await retry_all_stories()
    logger.info("retry-all: %d story:lang pairs enqueued", queued)
    return {"queued": queued}
