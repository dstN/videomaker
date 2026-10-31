import asyncio
import logging
from pathlib import Path
import os

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from app.database import SessionLocal
from app.models import Translation
from app.services import llm_service
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

async def process_one_missing(settings):
    """Finds and processes exactly one missing description."""
    db = SessionLocal()
    try:
        # Get all translations ordered by newest first
        translations = db.query(Translation).order_by(Translation.story_id.desc()).all()
        
        for trans in translations:
            story = trans.story
            if not story:
                continue
                
            story_id = trans.story_id
            language_code = trans.language_code
            
            # Check if the story is already fully completed (meaning folder was likely deleted)
            all_completed = True
            has_jobs = False
            for part in trans.video_parts:
                for job in part.render_jobs:
                    has_jobs = True
                    if job.status.name != "completed" and job.status != "completed":
                        all_completed = False
            
            if has_jobs and all_completed:
                continue
            
            desc_path = settings.output_dir / language_code / str(story_id) / "description.txt"
            
            if not desc_path.exists():
                logger.info(f"Generating description for story_id={story_id} ({language_code})...")
                try:
                    desc_path.parent.mkdir(parents=True, exist_ok=True)
                    description = await llm_service.generate_description(
                        story.title or "Untitled",
                        trans.translated_text,
                        language_code,
                    )
                    desc_path.write_text(description, encoding="utf-8")
                    logger.info(f"Saved description -> {desc_path}")
                    return True # Success
                except Exception as e:
                    logger.error(f"Error generating description for story {story_id}: {e}")
                    if "429" in str(e) or "exhausted" in str(e).lower():
                        logger.info("Rate limit hit. Waiting 20s.")
                        await asyncio.sleep(20)
                    return False
        
        return None # Nothing found to process
    finally:
        db.close()

async def main():
    settings = get_settings()
    logger.info("Description generator daemon started. Monitoring for new stories...")
    
    while True:
        result = await process_one_missing(settings)
        
        if result is True:
            # We processed one, wait a bit for rate limits then check again immediately
            await asyncio.sleep(4.5)
        elif result is False:
            # An error occurred, wait a bit longer
            await asyncio.sleep(10)
        else:
            # result is None, nothing to do
            logger.info("All descriptions up to date. Sleeping for 5 minutes...")
            await asyncio.sleep(300)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user.")
