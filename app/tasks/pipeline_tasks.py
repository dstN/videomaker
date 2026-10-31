from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
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

# Backoff for stories whose preparation (translate/split via Gemini) keeps
# failing — without it the poller would re-hit Gemini every _POLL_INTERVAL.
_PREPARE_RETRY_BASE_DELAY = 60     # seconds after the first failure
_PREPARE_RETRY_MAX_DELAY = 3600    # cap for the exponential backoff
_PREPARE_MAX_ATTEMPTS = 5          # then the poller gives up until API restart
_prepare_failures: dict[str, tuple[int, float]] = {}  # key -> (failures, next attempt monotonic)


def _story_key(story_id: int, language_code: str) -> str:
    return f"{story_id}:{language_code}"


def _resume_status(part: VideoPart) -> JobStatus:
    """Furthest status a job can resume from, based on artefacts on disk."""
    if part.timestamps_path and Path(part.timestamps_path).exists():
        return JobStatus.sync_done
    if part.audio_path and Path(part.audio_path).exists():
        return JobStatus.audio_done
    return JobStatus.pending


def _in_prepare_backoff(key: str) -> bool:
    entry = _prepare_failures.get(key)
    if entry is None:
        return False
    failures, next_attempt = entry
    return failures >= _PREPARE_MAX_ATTEMPTS or time.monotonic() < next_attempt


def _record_prepare_result(story_id: int, language_code: str) -> None:
    """After a pipeline run: back off if the story still has no parts, else clear."""
    key = _story_key(story_id, language_code)
    db: Session = SessionLocal()
    try:
        has_parts = (
            db.query(VideoPart.id)
            .join(Translation, Translation.id == VideoPart.translation_id)
            .filter(
                Translation.story_id == story_id,
                Translation.language_code == language_code,
            )
            .first()
            is not None
        )
    finally:
        db.close()

    if has_parts:
        _prepare_failures.pop(key, None)
        return

    failures = _prepare_failures.get(key, (0, 0.0))[0] + 1
    delay = min(_PREPARE_RETRY_BASE_DELAY * 2 ** (failures - 1), _PREPARE_RETRY_MAX_DELAY)
    _prepare_failures[key] = (failures, time.monotonic() + delay)
    if failures >= _PREPARE_MAX_ATTEMPTS:
        logger.error(
            "Story %d/%s: no parts after %d attempts — poller gives up until restart. "
            "Retry manually via POST /pipeline/run.",
            story_id, language_code, failures,
        )
        return
    logger.warning(
        "Story %d/%s still has no parts after %d attempt(s) — next try in %ds.",
        story_id, language_code, failures, delay,
    )


def reset_interrupted_jobs() -> int:
    """
    Reset jobs left in 'rendering' by a crash/restart to their resume point.
    Must run before the worker starts — at that moment nothing is rendering.
    """
    db: Session = SessionLocal()
    try:
        jobs = db.query(RenderJob).filter(RenderJob.status == JobStatus.rendering).all()
        for job in jobs:
            job.status = _resume_status(job.video_part)
            logger.info("Reset interrupted job %d → %s", job.id, job.status)
        db.commit()
        return len(jobs)
    finally:
        db.close()


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
        job.status = _resume_status(part)
        job.error_message = None
        db.commit()
        logger.info("Reset job %d → %s (story=%d/%s)", job_id, job.status, story_id, lang)
    finally:
        db.close()

    return await enqueue_pipeline(story_id, lang)


async def retry_failed_jobs() -> int:
    """
    Reset every failed job of an existing story to its resume point and
    re-queue the affected story:lang pairs. Keeps all artefacts on disk —
    finished/exported parts are never touched. Returns pairs enqueued.
    """
    db: Session = SessionLocal()
    keys: set[tuple[int, str]] = set()
    try:
        jobs = (
            db.query(RenderJob)
            .join(VideoPart, VideoPart.id == RenderJob.video_part_id)
            .join(Translation, Translation.id == VideoPart.translation_id)
            .join(Story, Story.id == Translation.story_id)
            .filter(RenderJob.status == JobStatus.failed)
            .all()
        )
        for job in jobs:
            job.status = _resume_status(job.video_part)
            job.error_message = None
            tr = job.video_part.translation
            keys.add((tr.story_id, tr.language_code))
        db.commit()
    finally:
        db.close()

    queued = 0
    for story_id, lang in sorted(keys):
        if await enqueue_pipeline(story_id, lang):
            queued += 1
    logger.info("retry_failed_jobs: %d job(s) reset, %d story:lang pairs enqueued", len(jobs), queued)
    return queued


def _find_stories_with_pending_jobs() -> list[tuple[int, str]]:
    """
    Return (story_id, lang) pairs that need processing:
    - Stories with RenderJobs in a resumable state, OR
    - Translations that were never split into parts (split failed), OR
    - Stories that have no Translation at all yet (never started)

    Translations whose Story was deleted are ignored — SQLite does not enforce
    the FK, so such orphans would otherwise be re-enqueued forever.
    """
    db: Session = SessionLocal()
    try:
        results: list[tuple[int, str]] = []

        # 1. Stories with existing but unfinished jobs
        rows = (
            db.query(Translation.story_id, Translation.language_code)
            .join(Story, Story.id == Translation.story_id)
            .join(VideoPart, VideoPart.translation_id == Translation.id)
            .join(RenderJob, RenderJob.video_part_id == VideoPart.id)
            .filter(RenderJob.status.in_(_RESUMABLE_STATUSES))
            .distinct()
            .order_by(Translation.story_id)
            .all()
        )
        results.extend((r.story_id, r.language_code) for r in rows)

        # 2. Translations without any VideoPart (splitting never succeeded)
        unsplit = (
            db.query(Translation.story_id, Translation.language_code)
            .join(Story, Story.id == Translation.story_id)
            .filter(~Translation.video_parts.any())
            .order_by(Translation.story_id)
            .all()
        )
        results.extend((r.story_id, r.language_code) for r in unsplit)

        # 3. Stories that have no Translation at all (pipeline never started)
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
                if key not in _active_story_keys and not _in_prepare_backoff(key):
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
    reset = reset_interrupted_jobs()
    if reset:
        logger.info("Recovered %d job(s) interrupted mid-render.", reset)
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
            try:
                _record_prepare_result(story_id, language_code)
            except Exception:
                logger.exception("Could not record prepare result for %s.", key)
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


# ── Storing split parts (series for long stories) ─────────────────────────────

# Title suffix for stories split into a series ("<title> – Part 2")
_SERIES_LABEL = {"en": "Part", "de": "Teil", "fr": "Partie", "es": "Parte"}


def series_group_sizes(n_parts: int, max_parts: int) -> list[int]:
    """
    Distribute n_parts over as few stories as possible with at most max_parts
    each, front-loaded: 7 → [4, 3], 8 → [4, 4], 9 → [5, 4], 13 → [5, 4, 4].
    """
    if n_parts <= 0:
        return []
    groups = math.ceil(n_parts / max_parts)
    base, extra = divmod(n_parts, groups)
    return [base + 1] * extra + [base] * (groups - extra)


def store_parts(
    db: Session, story: Story, translation: Translation, raw_parts: list[dict]
) -> list[Story]:
    """
    Persist split parts (VideoPart + pending RenderJob) for a translation.

    A story with more than MAX_PARTS parts becomes a series: the first group
    stays on this story, every further group becomes a new Story linked via
    parent_story_id (followup_mode='continuation'), and all get a
    "<title> – Part N" title. New stories are picked up by the poller.
    Returns the stories of the series (just [story] if no split was needed).
    """
    settings = get_settings()
    lang = translation.language_code
    sizes = series_group_sizes(len(raw_parts), settings.max_parts)
    series: list[Story] = [story]

    start = 0
    for group_idx, size in enumerate(sizes):
        group = raw_parts[start:start + size]
        start += size
        group_text = "\n\n".join(p["text_snippet"] for p in group)
        is_last_group = group_idx == len(sizes) - 1

        if group_idx == 0:
            target = translation
        else:
            follow = Story(
                original_text_en=group_text,
                source_language=lang,
                source_url=story.source_url,
                voice=story.voice,
                parent_story_id=series[-1].id,
                followup_mode="continuation",
                # Every group but the last ends on its cliffhanger
                open_ending=story.open_ending if is_last_group else True,
            )
            db.add(follow)
            db.flush()
            target = Translation(story_id=follow.id, language_code=lang, translated_text=group_text)
            db.add(target)
            db.flush()
            series.append(follow)

        for number, raw in enumerate(group, 1):
            part = VideoPart(
                translation_id=target.id,
                part_number=number,
                text_snippet=raw["text_snippet"],
                cliffhanger_note=raw.get("cliffhanger_note", ""),
            )
            part.render_jobs.append(RenderJob(status=JobStatus.pending))
            db.add(part)

        if group_idx == 0 and len(sizes) > 1:
            # The first story now only covers its own group
            translation.translated_text = group_text
            if (story.source_language or "en") == lang:
                story.original_text_en = group_text
            story.open_ending = True

    if len(series) > 1:
        base_title = story.title or "Untitled"
        label = _SERIES_LABEL.get(lang, _SERIES_LABEL["en"])
        for number, member in enumerate(series, 1):
            member.title = f"{base_title} – {label} {number}"
        logger.info(
            "Story %d split into a series of %d stories (%s parts): %s",
            story.id, len(series), "/".join(map(str, sizes)), [s.id for s in series],
        )

    db.commit()
    return series


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def prepare_story_parts(story_id: int, language_code: str) -> None:
    """
    1. Assign voice
    2. Fetch or create Translation for language_code
    3. Split text into VideoParts via Gemini
    4. Create RenderJob stubs (long stories become a series, see store_parts)
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
                story.original_text_en, language_code, source_lang=story.source_language or "en"
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
                translation.translated_text, language_code, open_ending=bool(story.open_ending)
            )
            if title and not story.title:
                story.title = title
            store_parts(db, story, translation, raw_parts)
            logger.info("Created %d video parts.", len(raw_parts))
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
                job.status = _resume_status(part)
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
                    words = await whisperx_service.transcribe(
                        audio_path,
                        language_code,
                        text=tts_service.spoken_text(part.text_snippet, language_code),
                    )
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
                    loop = asyncio.get_running_loop()
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
                        language_code,
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
                except Exception as exc:
                    # Catch everything: an uncaught error here would leave the
                    # job stuck in 'rendering', which the poller never resumes.
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
