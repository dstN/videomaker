from app.models import JobStatus, RenderJob, Story, Translation, VideoPart
from app.tasks import pipeline_tasks


def _story_with_translation(db, story_id: int | None = None) -> Translation:
    story = Story(id=story_id, original_text_en="x" * 200, title="T")
    db.add(story)
    db.commit()
    tr = Translation(story_id=story.id, language_code="en", translated_text="text")
    db.add(tr)
    db.commit()
    return tr


def _add_part(db, tr: Translation, status: JobStatus, **part_kwargs) -> RenderJob:
    part = VideoPart(translation_id=tr.id, part_number=1, text_snippet="s", **part_kwargs)
    db.add(part)
    db.commit()
    job = RenderJob(video_part_id=part.id, status=status)
    db.add(job)
    db.commit()
    return job


def test_orphaned_translation_is_not_enqueued(db):
    # Translation + pending job whose story no longer exists (FK not enforced)
    tr = Translation(story_id=999, language_code="en", translated_text="text")
    db.add(tr)
    db.commit()
    _add_part(db, tr, JobStatus.pending)

    assert pipeline_tasks._find_stories_with_pending_jobs() == []


def test_story_with_pending_job_is_found(db):
    tr = _story_with_translation(db)
    _add_part(db, tr, JobStatus.pending)

    assert pipeline_tasks._find_stories_with_pending_jobs() == [(tr.story_id, "en")]


def test_translation_without_parts_is_found(db):
    tr = _story_with_translation(db)

    assert pipeline_tasks._find_stories_with_pending_jobs() == [(tr.story_id, "en")]


def test_story_without_translation_is_found(db):
    story = Story(original_text_en="x" * 200)
    db.add(story)
    db.commit()

    assert pipeline_tasks._find_stories_with_pending_jobs() == [(story.id, "en")]


def test_completed_story_is_not_found(db):
    tr = _story_with_translation(db)
    _add_part(db, tr, JobStatus.completed)

    assert pipeline_tasks._find_stories_with_pending_jobs() == []


def test_prepare_failure_backs_off_and_clears(db, monkeypatch):
    pipeline_tasks._prepare_failures.clear()
    tr = _story_with_translation(db)
    key = pipeline_tasks._story_key(tr.story_id, "en")

    pipeline_tasks._record_prepare_result(tr.story_id, "en")
    assert pipeline_tasks._in_prepare_backoff(key)
    first_failures, first_next = pipeline_tasks._prepare_failures[key]

    pipeline_tasks._record_prepare_result(tr.story_id, "en")
    failures, next_attempt = pipeline_tasks._prepare_failures[key]
    assert failures == first_failures + 1
    assert next_attempt > first_next  # exponential: delay grows

    _add_part(db, tr, JobStatus.pending)
    pipeline_tasks._record_prepare_result(tr.story_id, "en")
    assert not pipeline_tasks._in_prepare_backoff(key)
    assert key not in pipeline_tasks._prepare_failures


def test_backoff_delay_is_capped(db):
    pipeline_tasks._prepare_failures.clear()
    tr = _story_with_translation(db)
    key = pipeline_tasks._story_key(tr.story_id, "en")
    pipeline_tasks._prepare_failures[key] = (30, 0.0)  # 2**30 * base would be huge

    import time
    before = time.monotonic()
    pipeline_tasks._record_prepare_result(tr.story_id, "en")
    _, next_attempt = pipeline_tasks._prepare_failures[key]
    assert next_attempt - before <= pipeline_tasks._PREPARE_RETRY_MAX_DELAY + 1


def test_reset_interrupted_jobs_resumes_from_artefacts(db, tmp_dir):
    audio = tmp_dir / "part_001.wav"
    audio.write_bytes(b"RIFF")

    tr_audio = _story_with_translation(db)
    job_audio = _add_part(db, tr_audio, JobStatus.rendering, audio_path=str(audio))

    tr_none = _story_with_translation(db)
    job_none = _add_part(db, tr_none, JobStatus.rendering, audio_path="/missing.wav")

    tr_done = _story_with_translation(db)
    job_done = _add_part(db, tr_done, JobStatus.completed)

    assert pipeline_tasks.reset_interrupted_jobs() == 2

    db.expire_all()
    assert db.get(RenderJob, job_audio.id).status == JobStatus.audio_done
    assert db.get(RenderJob, job_none.id).status == JobStatus.pending
    assert db.get(RenderJob, job_done.id).status == JobStatus.completed
