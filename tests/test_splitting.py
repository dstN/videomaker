import asyncio
import re

from app.services import llm_service
from app.services.llm_service import plan_parts_locally, split_sentences

STORY = (
    "I sat in Tower 12 with a lukewarm cup of Folgers. The wind rattled the plexiglass.\n\n"
    "It was 6:14 AM. \"Is anyone out there?\" I asked. Static answered!\n"
    "Then a click… A man answered (barely). He said his name was Silas."
)


def _norm(s: str) -> list[str]:
    return re.sub(r"\W+", " ", s).lower().split()


def test_sentence_spans_cover_all_text_in_order():
    spans = split_sentences(STORY)
    rebuilt = " ".join(STORY[s:e] for s, e in spans)
    assert _norm(rebuilt) == _norm(STORY)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert STORY[spans[0][0]:spans[0][1]] == "I sat in Tower 12 with a lukewarm cup of Folgers."
    assert '"Is anyone out there?"' in [STORY[s:e] for s, e in spans]


def test_local_plan_respects_word_range():
    counts = [12, 9, 15, 20, 7, 11, 14, 18, 10, 13] * 8  # 1032 words
    ends = plan_parts_locally(counts, 160, 210)
    assert ends[-1] == len(counts)
    start = 0
    for end in ends:
        assert 160 <= sum(counts[start:end]) <= 210
        start = end


def test_local_plan_short_text_is_single_part():
    assert plan_parts_locally([30, 40], 160, 210) == [2]


def test_local_plan_isolates_oversized_sentence():
    ends = plan_parts_locally([100, 500, 100, 90], 160, 210)
    assert ends[-1] == 4
    assert 2 in ends and 1 in ends  # the 500-word sentence stands alone


def _long_story(sentences: int = 120) -> str:
    return " ".join(f"Sentence number {i} has exactly eight words here." for i in range(sentences))


class _FakeResponse:
    def __init__(self, parsed):
        self.parsed = parsed
        self.text = parsed.model_dump_json()


def _patch_llm(monkeypatch, plan):
    async def fake_resolve():
        return None, "m", [], None

    async def fake_generate(*_args, **_kwargs):
        if isinstance(plan, Exception):
            raise plan
        return _FakeResponse(plan)

    monkeypatch.setattr(llm_service, "_resolve_model_async", fake_resolve)
    monkeypatch.setattr(llm_service, "_generate_with_fallback", fake_generate)


def test_split_uses_valid_llm_plan_verbatim(monkeypatch):
    text = _long_story(48)  # 48 sentences × 8 words = 384 words
    plan = llm_service._SplitPlan(
        story_title="Eight Words",
        parts=[
            llm_service._SplitBoundary(end_sentence=24, cliffhanger_note="a"),
            llm_service._SplitBoundary(end_sentence=48, cliffhanger_note="RESOLUTION"),
        ],
    )
    _patch_llm(monkeypatch, plan)

    title, parts = asyncio.run(llm_service.split_into_parts(text))

    assert title == "Eight Words"
    assert [p["word_count"] for p in parts] == [192, 192]
    assert [p["cliffhanger_note"] for p in parts] == ["a", "RESOLUTION"]
    assert " ".join(p["text_snippet"] for p in parts) == text


def test_split_falls_back_when_plan_drops_text(monkeypatch):
    text = _long_story(120)
    # Plan stops early — the old prompt-based split silently lost text like this
    plan = llm_service._SplitPlan(
        story_title="T",
        parts=[llm_service._SplitBoundary(end_sentence=24, cliffhanger_note="x")],
    )
    _patch_llm(monkeypatch, plan)

    _, parts = asyncio.run(llm_service.split_into_parts(text))

    assert " ".join(p["text_snippet"] for p in parts) == text
    assert all(160 <= p["word_count"] <= 210 for p in parts)
    assert parts[-1]["cliffhanger_note"] == "RESOLUTION"


def test_split_falls_back_when_llm_errors(monkeypatch):
    text = _long_story(120)
    _patch_llm(monkeypatch, RuntimeError("500 INTERNAL"))

    title, parts = asyncio.run(llm_service.split_into_parts(text, open_ending=True))

    assert title is None
    assert " ".join(p["text_snippet"] for p in parts) == text
    assert all(p["cliffhanger_note"] == "" for p in parts)
