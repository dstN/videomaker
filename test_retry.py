import asyncio
import os
os.environ["DATA_DIR"] = "./data"
os.environ["ASSETS_DIR"] = "./assets"
os.environ["OUTPUT_DIR"] = "./output"
os.environ["DATABASE_URL"] = "sqlite:///./data/videomaker.db"
import logging
from app.tasks.pipeline_tasks import prepare_story_parts

logging.basicConfig(level=logging.INFO)

async def main():
    try:
        await prepare_story_parts(153, "en")
    except Exception as e:
        logging.error(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
