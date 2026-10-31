import asyncio
import json
import logging
from pathlib import Path
import os
import re

# Override environment variables for local script execution
# so that pydantic-settings uses local directories instead of root (/data)
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"

from app.database import SessionLocal
from app.models import Story
from app.services import llm_service
from app.tasks.pipeline_tasks import prepare_story_parts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CACHE_FILE = Path("data/reddit_cache.json")
TOTAL_MAX_STORIES = None  # Set to a number to limit, or None to process all cached stories


def clean_story_text(text: str) -> str:
    """
    Strip Reddit meta-commentary and edit links before passing to Gemini:
    - EDIT / UPDATE lines that link to Part 2, 3, etc.
    - Lines that are just reader-interaction hooks ("if you want more...", "let me know...")
    - Leftover markdown Reddit artifacts
    """
    # Remove EDIT/UPDATE lines (often add links to Part 2 or author notes)
    text = re.sub(r'\n+\*{0,2}(EDIT|UPDATE|Edit|Update)\b[^\n]*', '', text)
    # Remove lines linking to another reddit post / part
    text = re.sub(r'\n+[^\n]*(reddit\.com/r/\w+/comments/)[^\n]*', '', text)
    # Remove trailing reader-hooks (last 1-2 sentences that are meta)
    text = re.sub(
        r'(\n+.{0,200}(let me know|more tomorrow|more stories|thanks for reading|if anyone|theories|feel free)[^\n]*)',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # Strip leftover leading/trailing whitespace
    return text.strip()



async def main():
    if not CACHE_FILE.exists():
        logger.error(f"Cache file {CACHE_FILE} not found. Please run fetch_reddit.py first.")
        return

    try:
        cache = json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.error(f"Cache file {CACHE_FILE} is invalid or empty.")
        return

    if not cache:
        logger.info("Local cache is empty. Run fetch_reddit.py to fetch more stories.")
        return

    logger.info(f"Found {len(cache)} stories in local cache.")

    processed_count = 0
    
    # Pop up to TOTAL_MAX_STORIES from the front of the cache (None = all)
    batch_to_process = cache[:TOTAL_MAX_STORIES] if TOTAL_MAX_STORIES else cache
    remaining_cache = cache[len(batch_to_process):]

    db = SessionLocal()
    try:
        existing_urls = {row[0] for row in db.query(Story.source_url).all() if row[0]}
    finally:
        db.close()

    new_batch = []
    seen_in_batch = set()
    skipped_count = 0
    for post in batch_to_process:
        url = post["url_src"]
        if url in existing_urls or url in seen_in_batch:
            skipped_count += 1
        else:
            new_batch.append(post)
            seen_in_batch.add(url)

    if skipped_count > 0:
        logger.info(f"Skipped {skipped_count} stories that are already in the database or duplicates in batch.")

    for post in new_batch:
        title = post["title"]
        text = post["text"]
        url_src = post["url_src"]
        
        logger.info(f"Processing story: {title}")
        
        try:
            db = SessionLocal()
            try:
                # Rewrite the story text
                clean_text = clean_story_text(text)
                rewritten_title, rewritten_text = await llm_service.rewrite_story(title, clean_text)
                if rewritten_title and rewritten_text:
                    logger.info(f"Rewritten as: {rewritten_title}")
                    story = Story(
                        title=rewritten_title,
                        original_text_en=rewritten_text,
                        source_url=url_src,
                    )
                    db.add(story)
                    db.commit()
                    db.refresh(story)
                    
                    # Pre-chunk story into VideoParts so it's ready for TTS/Rendering
                    logger.info(f"Pre-chunking story {story.id}...")
                    await prepare_story_parts(story.id, "en")
            finally:
                db.close()
                
            processed_count += 1
            
            # Respect Gemini rate limit (15 RPM for Free Tier -> 1 request every 4 seconds)
            await asyncio.sleep(4.5)
            
        except Exception as e:
            logger.error(f"Error processing story '{title}': {e}")
            # Put the failed story back into the remaining cache so it's not lost
            remaining_cache.insert(0, post)
            
            # If a 429 rate limit or similar occurs, wait longer
            if "429" in str(e) or "exhausted" in str(e).lower():
                logger.info("Rate limit hit. Stopping ingestion for now.")
                break
            else:
                await asyncio.sleep(10)

    # Persist the remaining stories back to the cache
    logger.info(f"Finished processing {processed_count} stories.")
    logger.info(f"Saving {len(remaining_cache)} unprocessed stories back to local cache.")
    CACHE_FILE.write_text(json.dumps(remaining_cache, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
