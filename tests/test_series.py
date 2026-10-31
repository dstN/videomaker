import pytest

from app.models import JobStatus, Story, Translation
from app.tasks.pipeline_tasks import series_group_sizes, store_parts


@pytest.mark.parametrize("n, expected", [
    (1, [1]), (6, [6]), (7, [4, 3]), (8, [4, 4]), (9, [5, 4]), (10, [5, 5]),
    (12, [6, 6]), (13, [5, 4, 4]), (16, [6, 5, 5]),
])
def test_series_group_sizes(n, expected):
    assert series_group_sizes(n, 6) == expected


def _raw(n):
    return [
        {"part_number": i, "text_snippet": f"Snippet {i}.", "cliffhanger_note": f"note {i}"}
        for i in range(1, n + 1)
    ]


def _story(db, title="The Tower"):
    story = Story(original_text_en="full text " * 30, title=title, voice="casual_male",
                  source_url="https://example.com/x")
    db.add(story)
    db.flush()
    tr = Translation(story_id=story.id, language_code="en", translated_text=story.original_text_en)
    db.add(tr)
    db.flush()
    return story, tr


def test_short_story_is_stored_as_is(db):
    story, tr = _story(db)
    series = store_parts(db, story, tr, _raw(5))

    assert series == [story]
    assert story.title == "The Tower"
    assert [p.part_number for p in tr.video_parts] == [1, 2, 3, 4, 5]
    assert all(p.render_jobs[0].status == JobStatus.pending for p in tr.video_parts)


def test_long_story_becomes_linked_series(db):
    story, tr = _story(db)
    series = store_parts(db, story, tr, _raw(9))

    assert len(series) == 2
    first, second = series
    assert first.title == "The Tower – Part 1"
    assert second.title == "The Tower – Part 2"
    assert second.parent_story_id == first.id
    assert second.followup_mode == "continuation"
    assert second.voice == "casual_male"
    assert first.open_ending  # ends on the cliffhanger into part 2

    (tr2,) = second.translations
    assert [p.text_snippet for p in tr.video_parts] == [f"Snippet {i}." for i in range(1, 6)]
    assert [p.text_snippet for p in tr2.video_parts] == [f"Snippet {i}." for i in range(6, 10)]
    assert [p.part_number for p in tr2.video_parts] == [1, 2, 3, 4]
    # Each story's text only covers its own parts
    assert tr.translated_text == "\n\n".join(f"Snippet {i}." for i in range(1, 6))
    assert first.original_text_en == tr.translated_text
    assert tr2.translated_text == "\n\n".join(f"Snippet {i}." for i in range(6, 10))


def test_three_way_series_chains_parents(db):
    story, tr = _story(db)
    a, b, c = store_parts(db, story, tr, _raw(13))
    assert (b.parent_story_id, c.parent_story_id) == (a.id, b.id)
    assert [len(s.translations[0].video_parts) for s in (a, b, c)] == [5, 4, 4]
    assert c.title.endswith("– Part 3")


def test_german_series_label(db):
    story, _ = _story(db, title="Der Turm")
    tr = Translation(story_id=story.id, language_code="de", translated_text="x")
    db.add(tr)
    db.flush()
    first, second = store_parts(db, story, tr, _raw(7))
    assert second.title == "Der Turm – Teil 2"
    assert first.original_text_en == "full text " * 30  # source is en, translation de
