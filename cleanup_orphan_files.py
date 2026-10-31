import os
import shutil
from pathlib import Path
import logging

# Override environment variables for local script execution
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from app.database import SessionLocal
from app.models import Story
from app.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    settings = get_settings()
    db = SessionLocal()
    
    try:
        # Get all valid story IDs from the database
        valid_ids = {str(row[0]) for row in db.query(Story.id).all()}
        logger.info(f"Found {len(valid_ids)} valid stories in database.")
        
        output_dir = Path(settings.output_dir)
        if not output_dir.exists():
            logger.info("Output directory does not exist. Nothing to clean.")
            return

        removed_count = 0
        # Iterate through language folders (e.g., output/en, output/de)
        for lang_dir in output_dir.iterdir():
            if not lang_dir.is_dir():
                continue
                
            logger.info(f"Checking language directory: {lang_dir.name}")
            
            # Iterate through story folders (e.g., output/en/123)
            for story_dir in lang_dir.iterdir():
                if not story_dir.is_dir():
                    continue
                
                story_id_str = story_dir.name
                if story_id_str not in valid_ids:
                    logger.info(f"Removing orphan directory: {story_dir}")
                    try:
                        shutil.rmtree(story_dir)
                        removed_count += 1
                    except Exception as e:
                        logger.error(f"Failed to remove {story_dir}: {e}")
        
        logger.info(f"Cleanup complete. Removed {removed_count} orphan story directories.")
        
    finally:
        db.close()

if __name__ == "__main__":
    main()
