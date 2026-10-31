"""
Delete translations / video parts / render jobs whose story no longer exists.

SQLite did not enforce foreign keys before, so deleting stories left these
rows behind. Dry run by default — pass --apply to actually delete.
Rendered files of orphaned stories are handled by cleanup_orphan_files.py.

Usage:
    python cleanup_orphan_rows.py           # show what would be deleted
    python cleanup_orphan_rows.py --apply   # delete
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

ORPHAN_TRANSLATIONS = "SELECT id FROM translations WHERE story_id NOT IN (SELECT id FROM stories)"
ORPHAN_PARTS = (
    "SELECT id FROM video_parts WHERE translation_id NOT IN (SELECT id FROM translations) "
    f"OR translation_id IN ({ORPHAN_TRANSLATIONS})"
)
ORPHAN_JOBS = (
    "SELECT id FROM render_jobs WHERE video_part_id NOT IN (SELECT id FROM video_parts) "
    f"OR video_part_id IN ({ORPHAN_PARTS})"
)


def main() -> None:
    apply = "--apply" in sys.argv
    with engine.begin() as conn:
        counts = {
            name: conn.execute(text(f"SELECT COUNT(*) FROM ({sql})")).scalar()
            for name, sql in (
                ("render_jobs", ORPHAN_JOBS),
                ("video_parts", ORPHAN_PARTS),
                ("translations", ORPHAN_TRANSLATIONS),
            )
        }
        for name, count in counts.items():
            print(f"{name:13s} orphaned: {count}")

        if not apply:
            print("\nDry run — nothing deleted. Re-run with --apply to delete.")
            return

        # Children first; each subquery is evaluated before its rows go away.
        conn.execute(text(f"DELETE FROM render_jobs WHERE id IN ({ORPHAN_JOBS})"))
        conn.execute(text(f"DELETE FROM video_parts WHERE id IN ({ORPHAN_PARTS})"))
        conn.execute(text(f"DELETE FROM translations WHERE id IN ({ORPHAN_TRANSLATIONS})"))
        print("\nDeleted.")


if __name__ == "__main__":
    main()
