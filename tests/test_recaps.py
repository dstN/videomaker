import asyncio
from types import SimpleNamespace

from app.models import JobStatus, Story, Translation, VideoPart
from app.services import claude_service, llm_service
from app.tasks.pipeline_tasks import narration, store_parts


def _patch_structured(monkeypatch, result):
    async def fake(_prompt, _cls, temperature=0.7):
        if isinstance(result, Exception):
            raise result
        return llm_service._Recaps(recaps=result)

    monkeypatch.setattr(llm_service, "_generate_structured", fake)


def test_recaps_align_with_parts(monkeypatch):
    _patch_structured(monkeypatch, ["I'm still in the tower.", "The radio went dead an hour ago."])
    out = asyncio.run(llm_service.generate_recaps(["a", "b", "c"]))
    assert out == ["", "I'm still in the tower.", "The radio went dead an hour ago."]


def test_recaps_drop_overlong_and_wrong_count(monkeypatch):
    _patch_structured(monkeypatch, ["ok line", "word " * 40])
    assert asyncio.run(llm_service.generate_recaps(["a", "b", "c"])) == ["", "ok line", ""]

    _patch_structured(monkeypatch, ["only one"])
    assert asyncio.run(llm_service.generate_recaps(["a", "b", "c"])) == ["", "", ""]


def test_recap_failure_is_not_fatal(monkeypatch):
    _patch_structured(monkeypatch, RuntimeError("down"))
    assert asyncio.run(llm_service.generate_recaps(["a", "b"])) == ["", ""]


def test_split_reserves_recap_budget_and_attaches_recaps(monkeypatch):
    async def fake_resolve():
        return None, "m", [], None

    async def fake_generate(*_a, **_k):
        raise RuntimeError("no plan")  # forces the local splitter

    monkeypatch.setattr(llm_service, "_resolve_model_async", fake_resolve)
    monkeypatch.setattr(llm_service, "_generate_with_fallback", fake_generate)

    async def fake_recaps(parts, lang):
        return [""] + [f"recap {i}" for i in range(2, len(parts) + 1)]

    monkeypatch.setattr(llm_service, "generate_recaps", fake_recaps)

    text = " ".join(f"Sentence number {i} has exactly eight words here." for i in range(300))
    _, parts = asyncio.run(llm_service.split_into_parts(text))

    assert all(p["word_count"] <= 460 - 20 for p in parts)
    assert parts[0]["recap"] == ""
    assert parts[1]["recap"] == "recap 2"
    assert " ".join(p["text_snippet"] for p in parts) == text  # story text stays verbatim


def test_store_parts_keeps_recap_and_narration_speaks_it(db):
    story = Story(original_text_en="x " * 60, title="T")
    db.add(story)
    db.flush()
    tr = Translation(story_id=story.id, language_code="en", translated_text="x")
    db.add(tr)
    db.flush()
    store_parts(db, story, tr, [
        {"part_number": 1, "text_snippet": "First.", "cliffhanger_note": "", "recap": ""},
        {"part_number": 2, "text_snippet": "Second.", "cliffhanger_note": "", "recap": "I am 9114."},
    ])
    first, second = tr.video_parts
    assert first.recap is None and narration(first) == "First."
    assert second.recap == "I am 9114."
    assert narration(second) == "I am 9114.\n\nSecond."
