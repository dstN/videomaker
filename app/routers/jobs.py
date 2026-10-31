from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import JobStatus, RenderJob, VideoPart, Translation, Story
from app.schemas import RenderJobList, RenderJobResponse
from app.tasks.pipeline_tasks import enqueue_retry, retry_failed_jobs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/", response_model=RenderJobList)
def list_jobs(
    status: JobStatus | None = None,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> RenderJobList:
    query = (
        db.query(RenderJob, VideoPart, Translation, Story)
        .join(VideoPart, VideoPart.id == RenderJob.video_part_id)
        .join(Translation, Translation.id == VideoPart.translation_id)
        .join(Story, Story.id == Translation.story_id)
    )
    if status is not None:
        query = query.filter(RenderJob.status == status)
    total = query.count()
    rows = query.order_by(RenderJob.updated_at.desc()).offset(offset).limit(limit).all()
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
    return RenderJobList(jobs=result, total=total)


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


@router.post("/retry-failed")
async def retry_failed() -> dict:
    """Re-queue every failed job from its last good step. Deletes nothing."""
    queued = await retry_failed_jobs()
    logger.info("retry-failed: %d story:lang pairs enqueued", queued)
    return {"queued": queued}
