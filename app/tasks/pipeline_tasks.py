from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import JobStatus, RenderJob, Story, Translation, VideoPart
from app.services import llm_service, render_service, tts_service, whisperx_service

logger = logging.getLogger(__name__)


# ── Global serialisation queue ────────────────────────────────────────────────
# Only ONE pipeline runs at a time to avoid RAM/VRAM exhaustion.
# Deduplication is at STORY level ("story_id:lang") — because run_pipeline
# always processes the whole story regardless of which job triggered it.

_pipeline_queue: asyncio.Queue[tuple] = asyncio.Queue()
_active_story_keys: set[str] = set()   # "story_id:lang" of stories in queue/running

# Statuses that mean "unfinished, should be picked up automatically"
_RESUMABLE_STATUSES = {JobStatus.pending, JobStatus.audio_done, JobStatus.sync_done}
_POLL_INTERVAL = 3  # seconds between DB scans


def _story_key(story_id: int, language_code: str) -> str:
    return f"{story_id}:{language_code}"


async def enqueue_pipeline(story_id: int, language_code: str) -> bool:
    """
    Queue a full story pipeline run. Returns False if story is already queued.
    """
    key = _story_key(story_id, language_code)
    if key in _active_story_keys:
        logger.info("Pipeline story_id=%d lang=%s already queued — suppressed.", story_id, language_code)
        return False
    _active_story_keys.add(key)
    logger.info("Enqueued pipeline: story_id=%d lang=%s  (queue depth=%d)",
                story_id, language_code, _pipeline_queue.qsize())
    await _pipeline_queue.put(("pipeline", story_id, language_code))
    return True


async def enqueue_retry(job_id: int) -> bool:
    """
    Reset a specific job's status and queue its story for re-processing.
    Deduplicates at story level — returns False if story is already queued.
    """
    db: Session = SessionLocal()
    try:
        job: RenderJob | None = db.get(RenderJob, job_id)
        if job is None:
            logger.warning("enqueue_retry: job %d not found.", job_id)
            return False
        part: VideoPart = job.video_part
        story_id = part.translation.story_id
        lang = part.translation.language_code

        # Reset this job to the furthest resumable point
        if part.timestamps_path and Path(part.timestamps_path).exists():
            job.status = JobStatus.sync_done
        elif part.audio_path and Path(part.audio_path).exists():
            job.status = JobStatus.audio_done
        else:
            job.status = JobStatus.pending
        job.error_message = None
        db.commit()
        logger.info("Reset job %d → %s (story=%d/%s)", job_id, job.status, story_id, lang)
    finally:
        db.close()

    return await enqueue_pipeline(story_id, lang)


async def retry_all_stories() -> int:
    """
    Reset ALL translations (TTS + sync + render artefacts) and re-queue every
    story:lang pair. Returns the number of pairs successfully enqueued.
    """
    settings = get_settings()
    db: Session = SessionLocal()
    queued = 0
    try:
        translations = db.query(Translation).all()
        for tr in translations:
            story_id = tr.story_id
            lang = tr.language_code
            for part in tr.video_parts:
                if part.audio_path:
                    Path(part.audio_path).unlink(missing_ok=True)
                    part.audio_path = None
                if part.timestamps_path:
                    Path(part.timestamps_path).unlink(missing_ok=True)
                    part.timestamps_path = None
                for job in part.render_jobs:
                    if job.file_path:
                        Path(job.file_path).unlink(missing_ok=True)
                        job.file_path = None
                    job.status = JobStatus.pending
                    job.error_message = None
            db.commit()
            # Remove the .done sentinel so the story can be re-exported
            (settings.output_dir / lang / str(story_id) / ".done").unlink(missing_ok=True)
            if await enqueue_pipeline(story_id, lang):
                queued += 1
        logger.info("retry_all_stories: %d story:lang pairs enqueued", queued)
        return queued
    finally:
        db.close()


def _find_stories_with_pending_jobs() -> list[tuple[int, str]]:
    """
    Return (story_id, lang) pairs that need processing:
    - Stories with RenderJobs in a resumable state, OR
    - Stories that have no Translation at all yet (never started)
    """
    db: Session = SessionLocal()
    try:
        results: list[tuple[int, str]] = []

        # 1. Stories with existing but unfinished jobs
        rows = (
            db.query(Translation.story_id, Translation.language_code)
            .join(VideoPart, VideoPart.translation_id == Translation.id)
            .join(RenderJob, RenderJob.video_part_id == VideoPart.id)
            .filter(RenderJob.status.in_(_RESUMABLE_STATUSES))
            .distinct()
            .order_by(Translation.story_id)
            .all()
        )
        results.extend((r.story_id, r.language_code) for r in rows)

        # 2. Stories that have no Translation at all (pipeline never started)
        seen_story_ids = {r[0] for r in results}
        untranslated = (
            db.query(Story)
            .filter(~Story.translations.any())
            .order_by(Story.id)
            .all()
        )
        for story in untranslated:
            if story.id not in seen_story_ids:
                results.append((story.id, "en"))  # default to English

        return results
    finally:
        db.close()


async def _poll_pending_jobs() -> None:
    """
    Background coroutine: every _POLL_INTERVAL seconds find stories with
    unfinished jobs that aren't already queued, and enqueue them.
    """
    await asyncio.sleep(_POLL_INTERVAL)  # let startup settle first
    while True:
        try:
            for story_id, lang in _find_stories_with_pending_jobs():
                key = _story_key(story_id, lang)
                if key not in _active_story_keys:
                    logger.info("Poller: found pending story %d/%s — auto-enqueuing.", story_id, lang)
                    _active_story_keys.add(key)
                    await _pipeline_queue.put(("pipeline", story_id, lang))
        except Exception:
            logger.exception("Poller: error scanning for pending jobs.")
        await asyncio.sleep(_POLL_INTERVAL)


async def pipeline_worker() -> None:
    """
    Single long-running worker: processes one story pipeline at a time.
    Spawns the background poller on startup.
    """
    logger.info("Pipeline worker started — sequential job processing active.")
    asyncio.create_task(_poll_pending_jobs())
    while True:
        item = await _pipeline_queue.get()
        _, story_id, language_code = item
        key = _story_key(story_id, language_code)
        try:
            logger.info("Worker: starting pipeline story_id=%d lang=%s", story_id, language_code)
            await run_pipeline(story_id, language_code)
        except Exception:
            logger.exception("Pipeline worker caught unhandled exception — continuing.")
        finally:
            _active_story_keys.discard(key)
            _pipeline_queue.task_done()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_job_status(
    db: Session,
    job: RenderJob,
    status: JobStatus,
    error_message: str | None = None,
    file_path: str | None = None,
) -> None:
    job.status = status
    if error_message is not None:
        job.error_message = error_message
    if file_path is not None:
        job.file_path = file_path
    db.commit()


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def prepare_story_parts(story_id: int, language_code: str) -> None:
    """
    1. Assign voice
    2. Fetch or create Translation for language_code
    3. Split text into VideoParts via Gemini
    4. Create RenderJob stubs
    """
    db: Session = SessionLocal()
    try:
        story: Story | None = db.get(Story, story_id)
        if story is None:
            logger.error("Story %d not found — aborting prepare.", story_id)
            return

        if not story.voice:
            story.voice = random.choice(tts_service.AVAILABLE_VOICES)
            db.commit()
            logger.info("Story %d assigned voice: %s", story_id, story.voice)
            
        translation = (
            db.query(Translation)
            .filter_by(story_id=story_id, language_code=language_code)
            .first()
        )

        if translation is None:
            logger.info("Translating story %d → %s", story_id, language_code)
            translated_text = await llm_service.translate_text(
                story.original_text_en, language_code
            )
            translation = Translation(
                story_id=story_id,
                language_code=language_code,
                translated_text=translated_text,
            )
            db.add(translation)
            db.commit()
            db.refresh(translation)
            logger.info("Translation created (id=%d)", translation.id)

        existing_parts = (
            db.query(VideoPart)
            .filter_by(translation_id=translation.id)
            .order_by(VideoPart.part_number)
            .all()
        )

        if not existing_parts:
            logger.info("Splitting translation %d into parts...", translation.id)
            title, raw_parts = await llm_service.split_into_parts(
                translation.translated_text, language_code
            )
            if title and not story.title:
                story.title = title
                db.commit()
            for raw in raw_parts:
                part = VideoPart(
                    translation_id=translation.id,
                    part_number=raw["part_number"],
                    text_snippet=raw["text_snippet"],
                    cliffhanger_note=raw.get("cliffhanger_note", ""),
                )
                db.add(part)
            db.commit()
            existing_parts = (
                db.query(VideoPart)
                .filter_by(translation_id=translation.id)
                .order_by(VideoPart.part_number)
                .all()
            )
            logger.info("Created %d video parts.", len(existing_parts))

            for part in existing_parts:
                if not part.render_jobs:
                    db.add(RenderJob(video_part_id=part.id, status=JobStatus.pending))
            db.commit()
    finally:
        db.close()


async def run_pipeline(story_id: int, language_code: str) -> None:
    """
    Full idempotent pipeline:
      1. Prepare story parts (translation, split, job stubs)
      2. For each VideoPart/RenderJob, resume from current status
    """
    await prepare_story_parts(story_id, language_code)
    
    settings = get_settings()
    db: Session = SessionLocal()

    try:
        story: Story | None = db.get(Story, story_id)
        if story is None:
            return
            
        story_voice: str = story.voice
        translation = (
            db.query(Translation)
            .filter_by(story_id=story_id, language_code=language_code)
            .first()
        )
        if translation is None:
            return

        # ── Step 2: Retrieve split parts ──────────────────────────────────
        existing_parts = (
            db.query(VideoPart)
            .filter_by(translation_id=translation.id)
            .order_by(VideoPart.part_number)
            .all()
        )
        total_parts = len(existing_parts)
        story_title: str = story.title or ""

        # ── Description (idempotent) ──────────────────────────────────────
        desc_path = (
            settings.output_dir / language_code / str(story_id) / "description.txt"
        )
        if not desc_path.exists():
            try:
                desc_path.parent.mkdir(parents=True, exist_ok=True)
                description = await llm_service.generate_description(
                    story_title or "Untitled",
                    translation.translated_text,
                    language_code,
                )
                desc_path.write_text(description, encoding="utf-8")
                logger.info("Description saved → %s", desc_path)
            except Exception as exc:
                logger.warning("Description generation failed (non-fatal): %s", exc)

        for part in existing_parts:
            # Refresh part to get latest audio_path / timestamps_path
            db.refresh(part)
            job: RenderJob | None = (
                db.query(RenderJob)
                .filter_by(video_part_id=part.id)
                .first()
            )
            if job is None:
                job = RenderJob(video_part_id=part.id, status=JobStatus.pending)
                db.add(job)
                db.commit()
                db.refresh(job)

            if job.status == JobStatus.completed:
                logger.info("Part %d already completed — skipping.", part.part_number)
                continue

            if job.status == JobStatus.failed:
                logger.info(
                    "Part %d previously failed — retrying from last good status.",
                    part.part_number,
                )
                # Determine resume point based on stored paths
                if part.timestamps_path and Path(part.timestamps_path).exists():
                    job.status = JobStatus.sync_done
                elif part.audio_path and Path(part.audio_path).exists():
                    job.status = JobStatus.audio_done
                else:
                    job.status = JobStatus.pending
                job.error_message = None
                db.commit()

            # ── TTS ───────────────────────────────────────────────────────
            if job.status == JobStatus.pending:
                audio_dir = (
                    settings.output_dir / language_code / str(story_id) / "audio"
                )
                # Voxtral outputs WAV (16-bit PCM) — WhisperX reads this directly
                audio_path = audio_dir / f"part_{part.part_number:03d}.wav"

                try:
                    await tts_service.generate_audio(
                        part.text_snippet, audio_path, language_code, voice=story_voice
                    )
                    part.audio_path = str(audio_path)
                    db.commit()
                    _set_job_status(db, job, JobStatus.audio_done)
                except tts_service.TTSError as exc:
                    logger.error("TTS failed for part %d: %s", part.part_number, exc)
                    _set_job_status(db, job, JobStatus.failed, error_message=str(exc))
                    continue  # Pipeline continues for other parts

            # ── WhisperX ──────────────────────────────────────────────────
            if job.status == JobStatus.audio_done:
                audio_path = Path(part.audio_path)
                timestamps_dir = (
                    settings.output_dir / language_code / str(story_id) / "timestamps"
                )
                timestamps_path = timestamps_dir / f"part_{part.part_number:03d}.json"
                timestamps_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    words = await whisperx_service.transcribe(audio_path, language_code)
                    timestamps_path.write_text(json.dumps(words), encoding="utf-8")
                    part.timestamps_path = str(timestamps_path)
                    db.commit()
                    _set_job_status(db, job, JobStatus.sync_done)
                except whisperx_service.WhisperXError as exc:
                    logger.error(
                        "WhisperX failed for part %d: %s", part.part_number, exc
                    )
                    _set_job_status(db, job, JobStatus.failed, error_message=str(exc))
                    continue

            # ── Render ────────────────────────────────────────────────────
            if job.status == JobStatus.sync_done:
                _set_job_status(db, job, JobStatus.rendering)

                output_path = (
                    settings.output_dir
                    / language_code
                    / str(story_id)
                    / f"part_{part.part_number:03d}.mp4"
                )

                try:
                    # render_service.render_part is CPU/GPU-bound; run in thread pool
                    loop = asyncio.get_event_loop()
                    rendered_path = await loop.run_in_executor(
                        None,
                        render_service.render_part,
                        job.id,
                        part.part_number,
                        total_parts,
                        story_title,
                        Path(part.audio_path),
                        Path(part.timestamps_path),
                        output_path,
                    )
                    _set_job_status(
                        db,
                        job,
                        JobStatus.completed,
                        file_path=str(rendered_path),
                    )
                    logger.info(
                        "Part %d rendered → %s", part.part_number, rendered_path
                    )
                except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    logger.error(
                        "Render failed for part %d: %s", part.part_number, exc
                    )
                    _set_job_status(
                        db, job, JobStatus.failed, error_message=str(exc)
                    )

        # ── Write export sentinel when all parts completed ─────────────────
        remaining = (
            db.query(RenderJob)
            .join(VideoPart, RenderJob.video_part_id == VideoPart.id)
            .join(Translation, VideoPart.translation_id == Translation.id)
            .filter(
                Translation.story_id == story_id,
                Translation.language_code == language_code,
                RenderJob.status != JobStatus.completed,
            )
            .count()
        )
        if remaining == 0:
            story_dir = settings.output_dir / language_code / str(story_id)
            done_file = story_dir / ".done"
            if not done_file.exists():
                done_file.write_text(
                    json.dumps({"title": story_title}, ensure_ascii=False),
                    encoding="utf-8",
                )
                # chmod 777 so the WSL host user can delete the folder
                for item in story_dir.rglob("*"):
                    try:
                        os.chmod(item, 0o777)
                    except OSError:
                        pass
                os.chmod(story_dir, 0o777)
                logger.info(
                    "Story %d complete — .done sentinel written (export_watcher.sh will pick up).",
                    story_id,
                )

    except Exception as exc:
        logger.exception("Unhandled pipeline error for story %d: %s", story_id, exc)
    finally:
        db.close()


# ── Retry a single job ────────────────────────────────────────────────────────

async def retry_job(job_id: int) -> None:
    """
    Re-run the pipeline for a single failed RenderJob.
    Resets job to appropriate retry state and re-runs from there.
    """
    db: Session = SessionLocal()
    try:
        job: RenderJob | None = db.get(RenderJob, job_id)
        if job is None:
            logger.error("RenderJob %d not found.", job_id)
            return

        part: VideoPart = job.video_part
        translation: Translation = part.translation

        # Reset status based on available artifacts
        if part.timestamps_path and Path(part.timestamps_path).exists():
            job.status = JobStatus.sync_done
        elif part.audio_path and Path(part.audio_path).exists():
            job.status = JobStatus.audio_done
        else:
            job.status = JobStatus.pending
        job.error_message = None
        db.commit()

        await run_pipeline(translation.story_id, translation.language_code)
    finally:
        db.close()
