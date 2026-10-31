import json
import logging
from pathlib import Path
import os
import re
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATE_FILE = Path("reddit_fetch_state.json")
CACHE_FILE = Path("data/reddit_cache.json")
SUBREDDITS = ["nosleep", "creepypasta"]
MIN_LENGTH = 1000
FETCH_MAX_STORIES = 1000  # Number of valid stories to scrape per run — one big batch

def main():
    if not CACHE_FILE.parent.exists():
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {sub: {"after": None, "count": 0} for sub in SUBREDDITS}

    for sub in SUBREDDITS:
        if sub not in state:
            state[sub] = {"after": None, "count": 0}

    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            cache = []
    else:
        cache = []

    headers = {
        "User-Agent": "linux:videomaker_ingest:v1.0 (by /u/videomaker_bot)",
        "Accept": "application/json"
    }

    fetched_count = 0

    for sub in SUBREDDITS:
        if fetched_count >= FETCH_MAX_STORIES:
            break
        logger.info(f"--- Fetching stories for r/{sub} ---")
        
        while True:
            after = state[sub].get("after")
            url = f"https://www.reddit.com/r/{sub}/top.json?sort=top&t=all&limit=100"
            if after:
                url += f"&after={after}"
            
            logger.info(f"Requesting {url}")
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    status_code = r.status
                    body = r.read().decode('utf-8')
            except urllib.error.HTTPError as exc:
                logger.error(f"HTTP request failed: {exc.code}")
                break
            except Exception as exc:
                logger.error(f"HTTP request failed: {exc}")
                break
                
            if status_code != 200:
                logger.error(f"Failed to fetch (status {status_code})")
                break

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                logger.error("Failed to decode JSON from Reddit")
                break
                
            posts = data.get("data", {}).get("children", [])
            
            if not posts:
                logger.info(f"No more posts for r/{sub}")
                break

            last_seen_name = None
            added_this_page = 0
            for post in posts:
                post_data = post["data"]
                last_seen_name = post_data.get("name")
                title = post_data.get("title", "")
                text = post_data.get("selftext", "")
                url_src = "https://reddit.com" + post_data.get("permalink", "")
                
                if len(text) < MIN_LENGTH:
                    continue
                    
                # Skip multi-part series entirely
                if re.search(r'(?i)[\[\(]?part\s*\d+[\]\)]?', title) or re.search(r'(?i)\bpart\s*\d+\b', title) or re.search(r'(?i)(pt\.?\s*\d+)', title):
                    continue
                    
                cache.append({
                    "title": title,
                    "text": text,
                    "url_src": url_src,
                    "sub": sub
                })
                fetched_count += 1
                state[sub]["count"] = state[sub].get("count", 0) + 1
                added_this_page += 1
                
                if fetched_count >= FETCH_MAX_STORIES:
                    break

            if last_seen_name:
                state[sub]["after"] = last_seen_name
                STATE_FILE.write_text(json.dumps(state, indent=2))
                logger.info(f"Saved state for r/{sub}: count={state[sub].get('count', 0)}, after={last_seen_name}")
            
            logger.info(f"Added {added_this_page} valid stories to local cache. Total so far: {len(cache)}")
            CACHE_FILE.write_text(json.dumps(cache, indent=2))
            
            if fetched_count >= FETCH_MAX_STORIES or not data.get("data", {}).get("after"):
                break

    logger.info(f"Done. Cache now contains {len(cache)} stories.")

if __name__ == "__main__":
    main()
