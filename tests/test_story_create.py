from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.database import Base, engine, init_db
from app.main import app
from app.models import JobStatus, Story, Translation
from app.routers import stories as stories_router

STORY_TEXT = "Erster Teil der Geschichte. " * 10 + "\n\n" + "Zweiter Teil der Geschichte. " * 10


def _client(monkeypatch, enqueued):
    async def fake_enqueue(story_id, lang):
        enqueued.append((story_id, lang))
        return True

    monkeypatch.setattr(stories_router, "enqueue_pipeline", fake_enqueue)
    return TestClient(app)  # no context manager → startup (assets, worker) is skipped


def test_previewed_parts_are_stored_verbatim(db, monkeypatch):
    enqueued = []
    parts = [
        {"part_number": 1, "text_snippet": "Erster Teil.", "cliffhanger_note": "x"},
        {"part_number": 2, "text_snippet": "Zweiter Teil.", "cliffhanger_note": "RESOLUTION"},
    ]
    r = _client(monkeypatch, enqueued).post("/stories/", json={
        "original_text_en": STORY_TEXT,
        "source_language": "de",
        "languages": ["de"],
        "parts": parts,
    })
    assert r.status_code == 201, r.text
    story_id = r.json()["id"]
    assert r.json()["source_language"] == "de"

    story = db.get(Story, story_id)
    (tr,) = story.translations
    assert tr.language_code == "de"
    assert tr.translated_text == STORY_TEXT
    assert [p.text_snippet for p in tr.video_parts] == ["Erster Teil.", "Zweiter Teil."]
    assert all(p.render_jobs[0].status == JobStatus.pending for p in tr.video_parts)
    assert enqueued == [(story_id, "de")]


def test_story_without_parts_is_left_for_pipeline(db, monkeypatch):
    enqueued = []
    r = _client(monkeypatch, enqueued).post("/stories/", json={"original_text_en": STORY_TEXT})
    assert r.status_code == 201
    assert db.query(Translation).count() == 0
    assert enqueued == [(r.json()["id"], "en")]


def test_init_db_adds_missing_columns():
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        # Old schema without source_language
        conn.execute(text(
            "CREATE TABLE stories (id INTEGER PRIMARY KEY, original_text_en TEXT NOT NULL, "
            "source_url VARCHAR(2048), created_at DATETIME, title VARCHAR(512), voice VARCHAR(64), "
            "parent_story_id INTEGER, followup_mode VARCHAR(32), open_ending INTEGER NOT NULL DEFAULT 0)"
        ))
        conn.execute(text("INSERT INTO stories (id, original_text_en) VALUES (1, 'old')"))

    init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("stories")}
    assert "source_language" in columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source_language FROM stories WHERE id=1")).scalar() == "en"
