"""
Rewrite all stories marked by mark_for_rewrite.py with Claude.

Each marked story becomes a NEW story (the original and its exported videos
stay untouched). With the API running, the pipeline poller then splits,
renders and exports the new stories automatically.
Needs ANTHROPIC_API_KEY in .env. Dry run by default.

Usage:
    python rewrite_marked_stories.py                # show order + cost estimate
    python rewrite_marked_stories.py --apply        # rewrite all marked stories
    python rewrite_marked_stories.py --apply --limit 1   # try a single story first
"""
import asyncio
import logging
import os
import sys

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from app.database import SessionLocal, init_db
from app.tasks.rewrite_tasks import estimate_cost_usd, marked_stories, rewrite_marked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main() -> None:
    apply = "--apply" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    init_db()
    db = SessionLocal()
    try:
        todo = marked_stories(db)[:limit]
        for s in todo:
            print(f"#{s.id:<4} {s.title}" + (f"  [{s.followup_mode} of #{s.parent_story_id}]" if s.followup_mode else ""))
        print(f"\n{len(todo)} stories, estimated Claude cost ≈ ${estimate_cost_usd(db, todo):.2f}")
        if not apply:
            print("Dry run — nothing rewritten. Re-run with --apply.")
            return

        done = await rewrite_marked(db, limit)
        for old_id, new_id in done:
            print(f"#{old_id} → #{new_id}")
        print(f"\nRewrote {len(done)} of {len(todo)} stories.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
