"""
Re-split stories that still carry an old Gemini split and have not started yet.

The old splitter let Gemini rewrite the parts, which lost text and produced
parts far outside 160-210 words. This removes the parts + jobs of every
translation where no job has progressed (all 'pending', no audio yet), so
the pipeline poller re-splits them with the sentence-based splitter.
Costs one Gemini call per story when the poller picks it up.

Dry run by default — pass --apply to delete.

Usage:
    python resplit_unstarted_stories.py           # show what would be re-split
    python resplit_unstarted_stories.py --apply   # delete old parts/jobs
"""
import os
import sys

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from sqlalchemy import text

from app.database import engine

UNSTARTED_TRANSLATIONS = """
SELECT t.id FROM translations t
JOIN stories s ON s.id = t.story_id
WHERE EXISTS (SELECT 1 FROM video_parts v WHERE v.translation_id = t.id)
  AND NOT EXISTS (
    SELECT 1 FROM video_parts v
    JOIN render_jobs r ON r.video_part_id = v.id
    WHERE v.translation_id = t.id
      AND (r.status != 'pending' OR v.audio_path IS NOT NULL)
  )
"""


def main() -> None:
    apply = "--apply" in sys.argv
    with engine.begin() as conn:
        ids = [row[0] for row in conn.execute(text(UNSTARTED_TRANSLATIONS))]
        parts = conn.execute(text(
            f"SELECT COUNT(*) FROM video_parts WHERE translation_id IN ({UNSTARTED_TRANSLATIONS})"
        )).scalar()
        print(f"Unstarted translations: {len(ids)}  (video parts: {parts})")

        if not apply:
            print("\nDry run — nothing deleted. Re-run with --apply to delete.")
            return

        conn.execute(text(
            "DELETE FROM render_jobs WHERE video_part_id IN "
            f"(SELECT id FROM video_parts WHERE translation_id IN ({UNSTARTED_TRANSLATIONS}))"
        ))
        conn.execute(text(f"DELETE FROM video_parts WHERE translation_id IN ({UNSTARTED_TRANSLATIONS})"))
        print(f"\nDeleted parts of {len(ids)} translations — the poller will re-split them.")


if __name__ == "__main__":
    main()
