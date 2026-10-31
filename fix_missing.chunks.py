import asyncio
import logging
import os

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from sqlalchemy import func
from app.database import SessionLocal
from app.models import Story, Translation, VideoPart
from app.tasks.pipeline_tasks import prepare_story_parts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    db = SessionLocal()
    try:
        # Find stories that have no video parts associated with them
        # We join Story -> Translation -> VideoPart and check for count 0
        stories_to_fix = (
            db.query(Story)
            .outerjoin(Story.translations)
            .outerjoin(Translation.video_parts)
            .group_by(Story.id)
            .having(func.count(VideoPart.id) == 0)
            .all()
        )
        
        if not stories_to_fix:
            logger.info("No stories found with missing chunks.")
            return

        logger.info(f"Found {len(stories_to_fix)} stories with missing chunks. Fixing now...")

        for story in stories_to_fix:
            logger.info(f"Fixing chunks for Story {story.id}: {story.title}")
            try:
                # This will automatically skip translation if it already exists
                # and just generate the missing chunks.
                await prepare_story_parts(story.id, "en")
                
                # Small sleep to respect rate limits
                await asyncio.sleep(4.5)
            except Exception as e:
                logger.error(f"Failed to fix Story {story.id}: {e}")

    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
