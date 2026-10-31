"""
Claude rewrite of our own (non-Reddit) stories.

1. mark_for_rewrite() sets Story.rewrite_requested on own stories.
2. rewrite_marked() rewrites each marked story into a NEW Story
   (rewritten_from_id → original) and clears the mark. The original and its
   already exported videos stay untouched; the pipeline poller picks up the
   new story and splits, renders and exports it like any other.

Follow-ups are rewritten after their parent, with the rewritten parent as
context, and are linked to the rewritten parent.
"""
from __future__ import annotations

import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Story
from app.services import llm_service

logger = logging.getLogger(__name__)

# Rough cost model for claude-opus-5-5 ($4 / $20 per MTok, cache reads $0.20)
_TOKENS_PER_WORD = 1.35
_REFERENCE_TOKENS = 37_000      # extracted signs_of_ai_writing.pdf, cached
_OUTPUT_FACTOR = 3.0            # story text + adaptive thinking headroom
_PRICE_IN, _PRICE_OUT, _PRICE_CACHE_READ = 4.0, 20.0, 0.20


def own_story_candidates(db: Session) -> list[Story]:
    """Own stories (no source_url) that are neither rewrites nor already rewritten."""
    rewritten_ids = {
        row[0] for row in db.query(Story.rewritten_from_id).filter(Story.rewritten_from_id.isnot(None))
    }
    stories = (
        db.query(Story)
        .filter(Story.source_url.is_(None))
        .filter(Story.rewritten_from_id.is_(None))
        # Series members ("– Part 2") are rewritten as part of their first story
        .filter(or_(Story.followup_mode.is_(None), Story.followup_mode != "continuation"))
        .order_by(Story.id)
        .all()
    )
    return [s for s in stories if s.id not in rewritten_ids]


def mark_for_rewrite(db: Session, ids: list[int] | None = None) -> list[Story]:
    """Mark own stories (or only the given ids among them) for a Claude rewrite."""
    candidates = own_story_candidates(db)
    if ids is not None:
        wanted = set(ids)
        candidates = [s for s in candidates if s.id in wanted]
    for story in candidates:
        story.rewrite_requested = True
    db.commit()
    return candidates


def marked_stories(db: Session) -> list[Story]:
    """Marked stories, parents before their follow-ups."""
    marked = db.query(Story).filter(Story.rewrite_requested.is_(True)).all()
    by_id = {s.id: s for s in marked}

    def depth(story: Story) -> int:
        d, parent_id, seen = 0, story.parent_story_id, set()
        while parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            d += 1
            parent_id = by_id[parent_id].parent_story_id
        return d

    return sorted(marked, key=lambda s: (depth(s), s.id))


def full_text(db: Session, story: Story) -> str:
    """The story's whole text, including later parts if it was split into a series."""
    texts = [story.original_text_en]
    current, seen = story, {story.id}
    while True:
        nxt = (
            db.query(Story)
            .filter(Story.parent_story_id == current.id, Story.followup_mode == "continuation")
            .first()
        )
        if nxt is None or nxt.id in seen:
            break
        texts.append(nxt.original_text_en)
        seen.add(nxt.id)
        current = nxt
    return "\n\n".join(texts)


def rewritten_version(db: Session, story_id: int | None) -> Story | None:
    if story_id is None:
        return None
    return (
        db.query(Story)
        .filter(Story.rewritten_from_id == story_id)
        .order_by(Story.id.desc())
        .first()
    )


def estimate_cost_usd(db: Session, stories: list[Story]) -> float:
    """Rough upper estimate for rewriting the given stories (write + continuity pass)."""
    total = 0.0
    for story in stories:
        words = len(full_text(db, story).split())
        parent_words = len(story.parent.original_text_en.split()) if story.parent else 0
        story_tokens = words * _TOKENS_PER_WORD
        uncached_in = 2 * (story_tokens + parent_words * _TOKENS_PER_WORD) + 3_000
        total += (
            uncached_in * _PRICE_IN
            + 2 * _REFERENCE_TOKENS * _PRICE_CACHE_READ
            + story_tokens * _OUTPUT_FACTOR * _PRICE_OUT
        ) / 1_000_000
    # One cache write of the reference per batch (5-minute cache, 1.25x)
    return total + _REFERENCE_TOKENS * _PRICE_IN * 1.25 / 1_000_000


async def rewrite_story(db: Session, story: Story) -> Story:
    """Rewrite one marked story into a new Story and clear the mark."""
    parent = story.parent
    parent_new = rewritten_version(db, story.parent_story_id)
    context = parent_new or parent

    title, text = await llm_service.rewrite_own_story(
        title=story.title or "Untitled",
        text=full_text(db, story),
        lang=story.source_language or "en",
        open_ending=bool(story.open_ending),
        parent_title=context.title if context else None,
        parent_text=full_text(db, context) if context else None,
        followup_mode=story.followup_mode,
        require_claude=True,  # a silent Gemini rewrite would defeat the purpose
    )

    new_story = Story(
        original_text_en=text,
        source_language=story.source_language or "en",
        title=title or story.title,
        voice=story.voice,
        parent_story_id=(parent_new or parent).id if (parent_new or parent) else None,
        followup_mode=story.followup_mode,
        open_ending=story.open_ending,
        rewritten_from_id=story.id,
    )
    db.add(new_story)
    story.rewrite_requested = False
    db.commit()
    db.refresh(new_story)
    logger.info(
        "Story %d rewritten → Story %d %r (%d → %d words)",
        story.id, new_story.id, new_story.title,
        len(full_text(db, story).split()), len(text.split()),
    )
    return new_story


async def rewrite_marked(db: Session, limit: int | None = None) -> list[tuple[int, int]]:
    """Rewrite marked stories in dependency order. Returns (old_id, new_id) pairs."""
    done: list[tuple[int, int]] = []
    for story in marked_stories(db)[:limit]:
        try:
            new_story = await rewrite_story(db, story)
        except Exception:
            logger.exception("Rewrite of story %d failed — it stays marked.", story.id)
            db.rollback()
            continue
        done.append((story.id, new_story.id))
    return done
