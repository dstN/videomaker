from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Story, Translation
from app.schemas import (
    StoryCreate,
    StoryGeneratePreviewRequest,
    StoryGeneratePreviewResponse,
    FollowupPreviewRequest,
    PartPreview,
    StoryResponse,
)
from app.services import llm_service
from app.tasks.pipeline_tasks import enqueue_pipeline, store_parts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stories", tags=["stories"])


@router.post("/generate-preview", response_model=StoryGeneratePreviewResponse)
async def generate_story_preview(
    payload: StoryGeneratePreviewRequest,
) -> StoryGeneratePreviewResponse:
    """
    Call Gemini to generate a new r/nosleep story from a prompt and split it
    into video parts. Does NOT write anything to the database — pure preview.
    The UI shows the result and lets the user decide to submit to the pipeline.
    """
    title, parts = await llm_service.generate_and_split(
        payload.prompt, payload.language, open_ending=payload.open_ending
    )

    assembled_text = "\n\n".join(p["text_snippet"] for p in parts)
    total_words = sum(p.get("word_count", len(p["text_snippet"].split())) for p in parts)

    return StoryGeneratePreviewResponse(
        assembled_text=assembled_text,
        title=title or "",
        parts=[
            PartPreview(
                part_number=p["part_number"],
                text_snippet=p["text_snippet"],
                word_count=p.get("word_count", len(p["text_snippet"].split())),
                cliffhanger_note=p.get("cliffhanger_note", ""),
            )
            for p in parts
        ],
        total_parts=len(parts),
        total_words=total_words,
    )


@router.post("/followup-preview", response_model=StoryGeneratePreviewResponse)
async def generate_followup_preview(
    payload: FollowupPreviewRequest,
    db: Session = Depends(get_db),
) -> StoryGeneratePreviewResponse:
    """
    Generate a sequel to an existing story using its full text as context.
    Does NOT write anything to the database — pure preview.
    Submit the result via POST /stories/ as usual.
    """
    parent = db.get(Story, payload.parent_story_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Story {payload.parent_story_id} not found")

    parent_title = parent.title or f"Story #{parent.id}"
    title, parts = await llm_service.generate_followup(
        parent_title=parent_title,
        parent_text=parent.original_text_en,
        lang=payload.language,
        mode=payload.mode,
        open_ending=payload.open_ending,
    )

    assembled_text = "\n\n".join(p["text_snippet"] for p in parts)
    total_words = sum(p.get("word_count", len(p["text_snippet"].split())) for p in parts)

    _mode_labels = {"sequel": "Sequel", "prequel": "Prequel", "expansion": "Untold"}
    default_title = f"{parent_title} — {_mode_labels.get(payload.mode, 'Follow-up')}"

    return StoryGeneratePreviewResponse(
        assembled_text=assembled_text,
        title=title or default_title,
        parts=[
            PartPreview(
                part_number=p["part_number"],
                text_snippet=p["text_snippet"],
                word_count=p.get("word_count", len(p["text_snippet"].split())),
                cliffhanger_note=p.get("cliffhanger_note", ""),
            )
            for p in parts
        ],
        total_parts=len(parts),
        total_words=total_words,
    )


@router.post("/", response_model=StoryResponse, status_code=status.HTTP_201_CREATED)
async def create_story(
    payload: StoryCreate,
    db: Session = Depends(get_db),
) -> StoryResponse:
    story = Story(
        original_text_en=payload.original_text_en,
        source_language=payload.source_language,
        title=payload.title or None,
        source_url=payload.source_url,
        parent_story_id=payload.parent_story_id,
        followup_mode=payload.followup_mode,
        open_ending=payload.open_ending,
    )
    db.add(story)
    db.flush()

    if payload.parts:
        translation = Translation(
            story_id=story.id,
            language_code=payload.source_language,
            translated_text=payload.original_text_en,
        )
        db.add(translation)
        db.flush()
        parts = sorted(payload.parts, key=lambda p: p.part_number)
        store_parts(db, story, translation, [p.model_dump() for p in parts])

    db.commit()
    db.refresh(story)

    # Enqueue pipeline for each requested language
    for lang in payload.languages:
        await enqueue_pipeline(story.id, lang)

    logger.info("Story %d created, pipeline enqueued for %s", story.id, payload.languages)
    return story


@router.get("/", response_model=list[StoryResponse])
def list_stories(
    limit: int = Query(25, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Story]:
    return (
        db.query(Story)
        .order_by(Story.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{story_id}", response_model=StoryResponse)
def get_story(story_id: int, db: Session = Depends(get_db)) -> Story:
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(status_code=404, detail=f"Story {story_id} not found")
    return story
