"""
Mark our own stories (not from Reddit) for a complete Claude rewrite.

Own stories = no source_url, not a rewrite themselves, not rewritten yet.
Series members ("– Part 2") are covered by their first story.
Dry run by default — pass --apply to set the mark.

Usage:
    python mark_for_rewrite.py                 # list candidates
    python mark_for_rewrite.py --apply         # mark all candidates
    python mark_for_rewrite.py --apply 6 50    # mark only stories 6 and 50
"""
import os
import sys

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from app.database import SessionLocal, init_db
from app.tasks.rewrite_tasks import estimate_cost_usd, mark_for_rewrite, own_story_candidates


def main() -> None:
    apply = "--apply" in sys.argv
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()] or None

    init_db()  # adds the rewrite columns to an existing database
    db = SessionLocal()
    try:
        candidates = own_story_candidates(db)
        if ids is not None:
            candidates = [s for s in candidates if s.id in set(ids)]
        for s in candidates:
            mode = f" [{s.followup_mode} of #{s.parent_story_id}]" if s.followup_mode else ""
            flag = " (already marked)" if s.rewrite_requested else ""
            print(f"#{s.id:<4} {len(s.original_text_en.split()):>5} words  {s.title}{mode}{flag}")
        print(f"\n{len(candidates)} stories, estimated Claude cost ≈ ${estimate_cost_usd(db, candidates):.2f}")

        if not apply:
            print("Dry run — nothing marked. Re-run with --apply to mark them.")
            return
        marked = mark_for_rewrite(db, ids)
        print(f"Marked {len(marked)} stories. Run rewrite_marked_stories.py next.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
