from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Status Enum ───────────────────────────────────────────────────────────────

class JobStatus(str, enum.Enum):
    pending = "pending"
    audio_done = "audio_done"
    sync_done = "sync_done"
    rendering = "rendering"
    completed = "completed"
    failed = "failed"


# ── Models ────────────────────────────────────────────────────────────────────

class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    voice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_text_en: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Follow-up tracking: set when this story was generated from another
    parent_story_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("stories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    followup_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # 'sequel' | 'prequel' | 'expansion' | None
    open_ending: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
    )

    parent: Mapped[Story | None] = relationship(
        "Story", remote_side="Story.id", foreign_keys="Story.parent_story_id", lazy="select"
    )
    translations: Mapped[list[Translation]] = relationship(
        "Translation", back_populates="story", cascade="all, delete-orphan"
    )


class Translation(Base):
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("story_id", "language_code", name="uq_story_lang"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    story_id: Mapped[int] = mapped_column(Integer, ForeignKey("stories.id"), nullable=False)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)

    story: Mapped[Story] = relationship("Story", back_populates="translations")
    video_parts: Mapped[list[VideoPart]] = relationship(
        "VideoPart", back_populates="translation", cascade="all, delete-orphan"
    )


class VideoPart(Base):
    __tablename__ = "video_parts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    translation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("translations.id"), nullable=False
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    cliffhanger_note: Mapped[str] = mapped_column(String(512), nullable=True)
    audio_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    timestamps_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    translation: Mapped[Translation] = relationship("Translation", back_populates="video_parts")
    render_jobs: Mapped[list[RenderJob]] = relationship(
        "RenderJob", back_populates="video_part", cascade="all, delete-orphan"
    )


class RenderJob(Base):
    __tablename__ = "render_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    video_part_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("video_parts.id"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.pending, nullable=False
    )
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )

    video_part: Mapped[VideoPart] = relationship("VideoPart", back_populates="render_jobs")
