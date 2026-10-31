import asyncio
import json
from types import SimpleNamespace

import pytest

from app.models import Story
from app.services import claude_service, llm_service
from app.tasks import rewrite_tasks


# ── claude_service ────────────────────────────────────────────────────────────

def test_schema_closes_nested_objects():
    schema = claude_service._schema_for(llm_service._ContinuityReport)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["edits"]
    edit = schema["$defs"]["_ContinuityEdit"]
    assert edit["additionalProperties"] is False
    assert set(edit["required"]) == {"find", "replace", "reason"}


class _FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


def _message(text, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=20,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(
        model="claude-opus-5-5", usage=usage, stop_reason=stop_reason, stop_details=None,
        content=[SimpleNamespace(type="text", text=text)],
    )


def _fake_client(monkeypatch, messages, captured=None):
    queue = list(messages)

    def stream(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return _FakeStream(queue.pop(0))

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    monkeypatch.setattr(claude_service, "_get_client", lambda: client)
    monkeypatch.setattr(claude_service, "_load_ai_signs_text", lambda: "REFERENCE TEXT")


def test_generate_json_parses_and_sends_cached_reference(monkeypatch):
    captured = []
    _fake_client(monkeypatch, [_message(json.dumps({"story_title": "T", "text": "Body"}))], captured)

    draft = asyncio.run(claude_service.generate_json("RULES", "task", llm_service._StoryDraft))

    assert (draft.story_title, draft.text) == ("T", "Body")
    req = captured[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "REFERENCE TEXT" in req["system"][-1]["text"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_generate_json_raises_on_unusable_stop(monkeypatch, stop_reason):
    _fake_client(monkeypatch, [_message("{}", stop_reason)])
    with pytest.raises(claude_service.ClaudeError):
        asyncio.run(claude_service.generate_json("R", "t", llm_service._StoryDraft))


# ── llm_service routing & continuity ─────────────────────────────────────────

def test_apply_edits_only_unique_matches():
    text = "I held the Baofeng. Later the Motorola crackled. The radio. The radio."
    edits = [
        llm_service._ContinuityEdit(find="the Motorola", replace="the Baofeng", reason="same radio"),
        llm_service._ContinuityEdit(find="The radio.", replace="X", reason="ambiguous"),
    ]
    out = llm_service._apply_edits(text, edits)
    assert "Motorola" not in out
    assert out.count("The radio.") == 2


def test_claude_story_runs_continuity_pass(monkeypatch):
    monkeypatch.setattr(claude_service, "is_configured", lambda: True)
    _fake_client(monkeypatch, [
        _message(json.dumps({"story_title": "Tower", "text": "A Baofeng. A Motorola."})),
        _message(json.dumps({"edits": [{"find": "A Motorola.", "replace": "The Baofeng again.",
                                        "reason": "radio"}]})),
    ])
    title, text = asyncio.run(llm_service._generate_story_text("prompt"))
    assert title == "Tower"
    assert text == "A Baofeng. The Baofeng again."


def test_falls_back_to_gemini_when_claude_fails(monkeypatch):
    monkeypatch.setattr(claude_service, "is_configured", lambda: True)

    async def boom(*_a, **_k):
        raise claude_service.ClaudeError("refused")

    async def fake_resolve():
        return None, "m", [], None

    async def fake_gemini(*_a, **_k):
        return SimpleNamespace(parsed=llm_service._StoryDraft(story_title="G", text="gemini"), text="")

    monkeypatch.setattr(claude_service, "generate_json", boom)
    monkeypatch.setattr(llm_service, "_resolve_model_async", fake_resolve)
    monkeypatch.setattr(llm_service, "_generate_with_fallback", fake_gemini)

    assert asyncio.run(llm_service._generate_story_text("p")) == ("G", "gemini")
    with pytest.raises(claude_service.ClaudeError):
        asyncio.run(llm_service._generate_story_text("p", require_claude=True))


# ── rewrite_tasks ─────────────────────────────────────────────────────────────

def _story(db, **kw):
    story = Story(original_text_en=kw.pop("text", "word " * 50), **kw)
    db.add(story)
    db.commit()
    return story


def test_candidates_exclude_reddit_rewrites_and_series_members(db):
    own = _story(db, title="Own")
    _story(db, title="Reddit", source_url="https://reddit.com/x")
    done = _story(db, title="Done")
    _story(db, title="Done v2", rewritten_from_id=done.id)
    _story(db, title="Own – Part 2", parent_story_id=own.id, followup_mode="continuation")

    assert [s.title for s in rewrite_tasks.own_story_candidates(db)] == ["Own"]


def test_full_text_joins_series(db):
    first = _story(db, text="one")
    second = _story(db, text="two", parent_story_id=first.id, followup_mode="continuation")
    _story(db, text="three", parent_story_id=second.id, followup_mode="continuation")
    assert rewrite_tasks.full_text(db, first) == "one\n\ntwo\n\nthree"


def test_rewrite_marked_orders_parents_first_and_relinks(db, monkeypatch):
    parent = _story(db, title="Parent", voice="casual_male")
    child = _story(db, title="Child", parent_story_id=parent.id, followup_mode="sequel")
    rewrite_tasks.mark_for_rewrite(db)
    calls = []

    async def fake_rewrite(**kwargs):
        calls.append(kwargs)
        assert kwargs["require_claude"] is True
        return f"{kwargs['title']} v2", f"rewritten {kwargs['title']}"

    monkeypatch.setattr(llm_service, "rewrite_own_story", fake_rewrite)

    done = asyncio.run(rewrite_tasks.rewrite_marked(db))

    assert [old for old, _ in done] == [parent.id, child.id]
    new_parent = db.get(Story, done[0][1])
    new_child = db.get(Story, done[1][1])
    assert new_parent.rewritten_from_id == parent.id
    assert new_parent.voice == "casual_male"
    assert new_child.parent_story_id == new_parent.id  # linked to the rewritten parent
    assert calls[1]["parent_text"] == "rewritten Parent"  # rewritten parent as context
    assert not db.get(Story, parent.id).rewrite_requested
    assert rewrite_tasks.own_story_candidates(db) == []


def test_failed_rewrite_keeps_mark(db, monkeypatch):
    story = _story(db, title="S")
    rewrite_tasks.mark_for_rewrite(db)

    async def boom(**_k):
        raise claude_service.ClaudeError("down")

    monkeypatch.setattr(llm_service, "rewrite_own_story", boom)
    assert asyncio.run(rewrite_tasks.rewrite_marked(db)) == []
    db.expire_all()
    assert db.get(Story, story.id).rewrite_requested
