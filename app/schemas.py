from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import JobStatus


# ── Story ─────────────────────────────────────────────────────────────────────

class PartIn(BaseModel):
    part_number: int
    text_snippet: str = Field(..., min_length=1)
    cliffhanger_note: str = ""
    recap: str = ""


class StoryCreate(BaseModel):
    original_text_en: str = Field(..., min_length=100, description="Story text in source_language")
    source_language: str = Field(default="en", description="Language original_text_en is written in")
    title: str | None = None
    source_url: str | None = None
    languages: list[str] = Field(default=["en"], description="Language codes to process, e.g. ['en', 'de']")
    parent_story_id: int | None = None
    followup_mode: str | None = None  # 'sequel' | 'prequel' | 'expansion'
    open_ending: bool = False
    # Parts approved in the preview. Stored as-is for source_language so the
    # rendered videos match what was reviewed (no second Gemini split).
    parts: list[PartIn] | None = None


class StoryGeneratePreviewRequest(BaseModel):
    prompt: str = Field(..., min_length=10, description="Story premise / prompt for Gemini")
    language: str = Field(default="en", description="Language code, e.g. 'en' or 'de'")
    open_ending: bool = Field(default=False, description="End last part on a cliffhanger instead of a resolution")


class FollowupPreviewRequest(BaseModel):
    parent_story_id: int = Field(..., description="ID of the story to generate a follow-up for")
    language: str = Field(default="en", description="Language code for the follow-up")
    mode: str = Field(default="sequel", description="'sequel' | 'prequel' | 'expansion'")
    open_ending: bool = Field(default=False, description="End last part on a cliffhanger instead of a resolution")


class PartPreview(BaseModel):
    part_number: int
    text_snippet: str
    word_count: int
    cliffhanger_note: str
    recap: str = ""


class StoryGeneratePreviewResponse(BaseModel):
    assembled_text: str
    title: str
    parts: list[PartPreview]
    total_parts: int
    total_words: int


class StoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    original_text_en: str
    source_language: str = "en"
    source_url: str | None
    parent_story_id: int | None = None
    followup_mode: str | None = None
    open_ending: bool = False
    rewrite_requested: bool = False
    rewritten_from_id: int | None = None
    created_at: datetime
    translations: list[TranslationResponse] = []


# ── Translation ───────────────────────────────────────────────────────────────

class TranslationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    story_id: int
    language_code: str
    translated_text: str
    video_parts: list[VideoPartResponse] = []


# ── VideoPart ─────────────────────────────────────────────────────────────────

class VideoPartResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    translation_id: int
    part_number: int
    text_snippet: str
    cliffhanger_note: str | None
    recap: str | None = None
    audio_path: str | None
    timestamps_path: str | None
    render_jobs: list[RenderJobResponse] = []


# ── RenderJob ─────────────────────────────────────────────────────────────────

class RenderJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_part_id: int
    story_id: int | None = None
    story_title: str | None = None
    part_number: int | None = None
    status: JobStatus
    file_path: str | None
    error_message: str | None
    updated_at: datetime


class RenderJobList(BaseModel):
    jobs: list[RenderJobResponse]
    total: int


# ── Pipeline ──────────────────────────────────────────────────────────────────

class PipelineRunRequest(BaseModel):
    story_id: int
    language_code: str = "en"


class PipelineRunResponse(BaseModel):
    story_id: int
    language_code: str
    message: str


# ── Error ─────────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str


# ── Forward refs ──────────────────────────────────────────────────────────────
StoryResponse.model_rebuild()
TranslationResponse.model_rebuild()
VideoPartResponse.model_rebuild()
