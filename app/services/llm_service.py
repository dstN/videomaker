"""
LLM Service — Gemini 3.1 Flash via the official google-genai SDK (2026).

Architecture:
  - Single `genai.Client` singleton per process
  - PDF uploaded ONCE to the File API (in-process TTL: 47h), reused across calls
  - No server-side CachedContent (not available on Free Tier)
  - Each generation call passes [pdf_file, task_prompt] directly in contents
  - system_instruction sent per-call via GenerateContentConfig
  - Falls back gracefully to a plain model if the PDF is not on disk
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

from google import genai
from google.genai import types

from app.config import get_settings

logger = logging.getLogger(__name__)

# ── Anti-AI system instruction ────────────────────────────────────────────────
# Sent per-call via GenerateContentConfig.system_instruction.
_SYSTEM_INSTRUCTION = """You are a master horror story writer for Reddit's r/nosleep community.
Your writing is raw, personal, atmospheric, and deeply unsettling.

FORBIDDEN WORDS — never use any of these, not even once:
crucial, pivotal, significant, testament, tapestry, vibrant, landscape, underscore,
delve, unpack, it's worth noting, at the end of the day, game-changer, boundaries

FORBIDDEN TROPES — your output will be rejected if these clichés appear:
- Waking up at exactly 3 AM, 3:47 AM, 2:30 AM, or any "witching hour" time
- Opening with "I couldn't sleep" or "I heard something downstairs"
- The narrator checking their phone and seeing missed calls or a warning text
- Doors or floors that creak at the wrong moment as a cheap jump
- A child or old woman standing in a corner / end of a hallway
- Protagonists who are writers, journalists, or paranormal investigators
- The twist "it was in the house with me all along"
- Mirrors that show something different from reality

FORBIDDEN STRUCTURAL PATTERNS — your output will be rejected if these appear:
1. Negative parallelism: constructions like "Not only X, but also Y" — NEVER
2. Rule of Three adjectives: never use exactly three adjectives in a sequence
   BAD: "the room was dark, cold, and empty"
   GOOD: "the room was dark. Cold in a way that felt deliberate."
3. Formulaic section endings: never end a part with a moral, reflection, or summary
4. Rhetorical questions as cliffhangers: avoid "What could it mean?" type endings
5. Em-dash overuse: maximum one em-dash per paragraph

STYLE REQUIREMENTS:
- Write in first-person past tense exclusively
- Ground horror in hyper-specific, mundane details (brand names, exact times, physical sensations)
- Use sentence rhythm variation: mix short punchy sentences with longer ones
- Cliffhangers must create dread through implication, not cheap shock
- No purple prose. No melodrama. Restraint is more terrifying than excess.
- Times and dates must feel incidental and specific — never symbolically loaded (no 3 AM)"""

# ── In-process state ─────────────────────────────────────────────────────────
_client: genai.Client | None = None

# File API: re-upload after 47h (server TTL = 48h)
_pdf_file: Any = None
_pdf_upload_time: float = 0.0
_PDF_UPLOAD_TTL = 47 * 3600

# Temperature for creative story generation (high enough for variety, low enough
# that JSON structure stays valid).
_CREATIVE_TEMPERATURE = 1.4

# ── Story seed injection ──────────────────────────────────────────────────────
# Random anchors injected per-call so every generation has a different starting
# point, preventing the model from falling into the same statistical grooves.

_SEED_TIMES = [
    "6:14 AM", "11:38 PM", "4:52 PM", "8:07 AM", "1:23 PM", "9:41 PM",
    "2:55 AM", "7:30 PM", "10:18 AM", "5:03 PM", "12:47 PM", "3:29 AM",
]
_SEED_OCCUPATIONS = [
    "a night-shift warehouse supervisor", "a retired school librarian",
    "a veterinary technician", "a road surveyor", "a commercial diver",
    "a tax attorney", "a hospice nurse", "a train dispatcher",
    "a building inspector", "a competitive chess player", "a crop-duster pilot",
    "a court stenographer", "a marine biologist", "a pawnshop owner",
]
_SEED_SETTINGS = [
    "a converted industrial loft", "a rented farmhouse in February",
    "the break room of a big-box retailer", "a weekend cabin with no cell signal",
    "an upper-floor apartment in a 1970s block", "a double-wide trailer at the edge of a state forest",
    "a university archive basement", "a mobile home park off the interstate",
    "a house inherited from an estranged uncle", "the back office of a failing print shop",
]


def _make_story_seed() -> str:
    """Return a short, randomised anchor string to inject into generation prompts."""
    t = random.choice(_SEED_TIMES)
    occ = random.choice(_SEED_OCCUPATIONS)
    loc = random.choice(_SEED_SETTINGS)
    return (
        f"STORY SEED (you MUST anchor the story around these specifics — do not swap them out):\n"
        f"  • Narrator's occupation: {occ}\n"
        f"  • Primary setting: {loc}\n"
        f"  • First specific time mentioned in the story: {t}\n"
    )


# ── Client ────────────────────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=get_settings().gemini_api_key)
    return _client


# ── File API ──────────────────────────────────────────────────────────────────

def _get_pdf_file(client: genai.Client) -> Any | None:
    """
    Ensure signs_of_ai_writing.pdf is uploaded to the File API.

    Strategy (cheapest first):
      1. In-process cache — skip everything if within 47h TTL.
      2. Server-side list — reuse an ACTIVE upload from a previous process run.
      3. Fresh upload.

    Returns None if the PDF does not exist on disk.
    """
    global _pdf_file, _pdf_upload_time

    now = time.time()
    if _pdf_file is not None and (now - _pdf_upload_time) < _PDF_UPLOAD_TTL:
        return _pdf_file

    pdf_path = get_settings().ai_pdf_path
    if not pdf_path.exists():
        logger.warning(
            "signs_of_ai_writing.pdf not found at %s — running without PDF constraint.",
            pdf_path,
        )
        return None

    # Check for an existing active upload on the server
    try:
        for f in client.files.list():
            if getattr(f, "display_name", None) == "signs_of_ai_writing":
                state = getattr(f, "state", None)
                state_name = getattr(state, "name", str(state))
                if state_name == "ACTIVE":
                    logger.info("Reusing existing File API upload (name=%s)", f.name)
                    _pdf_file = f
                    _pdf_upload_time = now
                    return f
    except Exception as exc:
        logger.debug("File list check failed (%s) — will re-upload.", exc)

    logger.info("Uploading %s to File API...", pdf_path.name)
    uploaded = client.files.upload(
        file=str(pdf_path),
        config=types.UploadFileConfig(
            display_name="signs_of_ai_writing",
            mime_type="application/pdf",
        ),
    )
    _pdf_file = uploaded
    _pdf_upload_time = now
    logger.info("PDF uploaded (name=%s)", uploaded.name)
    return uploaded


# ── Model resolution ──────────────────────────────────────────────────────────

def _resolve_model() -> tuple[genai.Client, str, list[str], Any | None]:
    """
    Returns (client, model_name, [fallback_models], pdf_file_or_None).
    """
    client = _get_client()
    settings = get_settings()

    # Fallback chain: Primary -> Flash -> Gemma 4
    fallbacks = ["gemini-2.5-flash", "gemma-4-31b-it"]

    pdf_file = _get_pdf_file(client)
    if pdf_file is None:
        logger.warning("No PDF available — proceeding without anti-AI PDF constraint.")
    return client, settings.gemini_model, fallbacks, pdf_file


def _is_rate_limited(exc: Exception) -> bool:
    """Return True if the exception is a 429 / RESOURCE_EXHAUSTED rate-limit error."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "QUOTA_EXCEEDED" in msg


async def _generate_with_fallback(
    client: genai.Client,
    primary_model: str,
    fallback_models: list[str],
    contents: list,
    config: types.GenerateContentConfig,
) -> Any:
    """
    Call generate_content with a chain of fallbacks on 429 errors.
    """
    models_to_try = [primary_model] + fallback_models
    
    for i, model_name in enumerate(models_to_try):
        try:
            # Gemma/Local models often don't support multimodal File API inputs
            current_contents = contents
            if "gemma" in model_name.lower():
                text_only = [c for c in contents if isinstance(c, str)]
                current_contents = text_only if text_only else contents

            return await client.aio.models.generate_content(
                model=model_name,
                contents=current_contents,
                config=config,
            )
        except Exception as exc:
            if not _is_rate_limited(exc) or i == len(models_to_try) - 1:
                raise
            
            logger.warning(
                "Model %s rate-limited (429) — retrying with next fallback %s",
                model_name,
                models_to_try[i+1],
            )


def _parse_json_parts(raw: str) -> list[dict]:
    """Parse and validate Gemini JSON output, stripping markdown fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        parts: list[dict] = json.loads(raw)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            # The LLM appended garbage text after a valid JSON array.
            # e.pos is the index where the valid JSON ends and the garbage begins.
            valid_json = raw[:e.pos].strip()
            try:
                parts: list[dict] = json.loads(valid_json)
                logger.warning("Recovered from JSON 'Extra data' error by stripping trailing text.")
            except Exception:
                logger.error(f"Failed to parse JSON. Raw content was:\n{raw}\n---")
                raise e
        else:
            logger.error(f"Failed to parse JSON. Raw content was:\n{raw}\n---")
            raise e

    required_keys = {"part_number", "text_snippet", "cliffhanger_note"}
    for p in parts:
        missing = required_keys - set(p.keys())
        if missing:
            raise ValueError(f"LLM returned part missing keys {missing}: {p}")

    return parts


def _extract_title(parts: list[dict]) -> str | None:
    """Pull the story title from the first part if Gemini included it."""
    if parts:
        return parts[0].get("story_title") or None
    return None


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_and_split(
    prompt: str,
    lang: str = "en",
    open_ending: bool = False,
) -> tuple[str | None, list[dict]]:
    """
    Generate a new r/nosleep style story from a user prompt, then split it
    into video parts of 220-290 words each.

    open_ending=True  → last part ends on a cliffhanger (teases a sequel).
    open_ending=False → last part resolves the central threat (default).

    Returns:
        (story_title, parts) where parts is a list of
        [{part_number, text_snippet, word_count, cliffhanger_note}]
    """
    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, pdf_file = await loop.run_in_executor(None, _resolve_model)

    if open_ending:
        ending_rule = (
            "- The story does NOT need to resolve — it must end on a HARD, irresistible cliffhanger.\n"
            "  The final scene should cut off at the worst possible moment. Leave the central threat\n"
            "  unanswered. The viewer MUST feel compelled to watch Part 2.\n"
            "- cliffhanger_note for the last part: ≤15 words describing the unresolved dread\n"
            "  (do NOT write 'RESOLUTION' — write the actual cliffhanger tension)."
        )
    else:
        ending_rule = (
            "- The story MUST be complete with a real, concrete ending.\n"
            "- The final part must resolve the central threat or mystery — not leave it dangling.\n"
            "  'I heard her voice' or 'I still don't know what it was' are NOT endings. Go further.\n"
            "- cliffhanger_note: write 'RESOLUTION' for the last part."
        )

    task_prompt = (
        f"TASK: Write an original Reddit r/nosleep horror story in {lang.upper()} "
        f"based on this premise:\n\n\"{prompt}\"\n\n"
        f"{_make_story_seed()}\n\n"
        "Then immediately split the full story into sequential video parts.\n\n"
        f"STORY REQUIREMENTS:\n{ending_rule}\n\n"
        "SPLITTING RULES:\n"
        "- Each part: exactly 230 to 450 words (strictly enforced for 90-180s TTS audio)\n"
        "- All parts EXCEPT the last MUST end mid-tension on a genuine cliffhanger\n"
        "- Parts must be contiguous — no overlap, no gaps, no paraphrasing\n"
        "- part_number starts at 1\n"
        "- word_count is the exact word count of text_snippet\n"
        "- story_title: ONLY on part_number=1, a short evocative title (≤6 words) for the full story\n\n"
        "Return ONLY a JSON array — no prose, no markdown fences:\n"
        '[{"part_number": 1, "story_title": "The Title", "text_snippet": "...", "word_count": 250, "cliffhanger_note": "..."}, ...]'
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    parts = _parse_json_parts(response.text)
    title = _extract_title(parts)
    return title, parts


async def split_into_parts(
    text: str,
    lang: str = "en",
    open_ending: bool = False,
) -> tuple[str | None, list[dict]]:
    """
    Split existing story text into 220-290 word video parts.
    Snippets are verbatim from the input — no rewriting.

    open_ending=True  → last part ends on a cliffhanger.
    open_ending=False → last part resolves the central threat (default).

    Returns:
        (story_title, parts) where parts is a list of
        [{part_number, text_snippet, word_count, cliffhanger_note}]
    """
    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, pdf_file = await loop.run_in_executor(None, _resolve_model)

    if open_ending:
        last_part_rule = (
            "- The LAST part MUST end on a HARD cliffhanger — cut off at maximum tension.\n"
            "  Do NOT resolve the central threat. The viewer must feel compelled to watch Part 2.\n"
            "- cliffhanger_note for the last part: ≤15 words of the unresolved dread (NOT 'RESOLUTION')."
        )
    else:
        last_part_rule = (
            "- The LAST part MUST end with a concrete resolution of the central threat or mystery.\n"
            "  'I heard her voice' or 'I still don't know' are NOT resolutions — go further.\n"
            "- cliffhanger_note: write 'RESOLUTION' for the last part."
        )

    task_prompt = (
        f"TASK: Split the following story text ({lang.upper()}) into sequential video parts.\n\n"
        "SPLITTING RULES:\n"
        "- Each part: 200 to 400 words (CRITICAL: NEVER exceed 400 words to stay under the 3-minute video limit)\n"
        "- All parts EXCEPT the last MUST end on a suspenseful cliffhanger\n"
        f"{last_part_rule}\n"
        "- text_snippet must be verbatim from the original — no edits, no paraphrasing\n"
        "- word_count is the exact word count of text_snippet\n"
        "- part_number starts at 1\n"
        "- story_title: ONLY on part_number=1, a short evocative title (≤6 words) for the full story\n\n"
        f"STORY TEXT:\n{text}\n\n"
        "Return ONLY a JSON array — no prose, no markdown:\n"
        '[{"part_number": 1, "story_title": "The Title", "text_snippet": "...", "word_count": 250, "cliffhanger_note": "..."}]'
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    parts = _parse_json_parts(response.text)
    title = _extract_title(parts)
    return title, parts


async def generate_description(title: str, full_text: str, lang: str = "en") -> str:
    """
    Generate a YouTube/TikTok-style video description + 5 hashtags for a story.
    #nosleep is always included.  Returns a ready-to-paste plain-text string.
    """
    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, _ = await loop.run_in_executor(None, _resolve_model)

    lang_note = f" Write the description in {lang.upper()}." if lang != "en" else ""

    task_prompt = (
        f"You are writing a YouTube / TikTok video description for a horror short story.{lang_note}\n\n"
        f"Story title: {title}\n\n"
        f"Story excerpt (first 600 chars):\n{full_text[:600]}…\n\n"
        "Instructions:\n"
        "1. Write 2–4 compelling sentences (~100–150 words total). "
        "Hook the viewer, hint at the horror, give ZERO spoilers. "
        "Address the viewer directly ('You asked for it…', 'What would you do if…').\n"
        "2. On a NEW line, write exactly 5 hashtags. "
        "#nosleep MUST be one of them. Pick the rest based on story themes "
        "(e.g. #horror #scarystories #creepypasta #reddit).\n\n"
        "Output format — nothing else, no labels, no markdown:\n"
        "<description text>\n\n"
        "#nosleep #tag2 #tag3 #tag4 #tag5"
    )

    response = await _generate_with_fallback(
        client, model_name, fallback_models, [task_prompt],
        types.GenerateContentConfig(),
    )
    return response.text.strip()


# Mode labels shown in prompts
_FOLLOWUP_MODE_PROMPTS: dict[str, str] = {
    "sequel": (
        "TASK: Write a direct SEQUEL to the r/nosleep story below, in {lang}.\n\n"
        "PARENT STORY (read-only context — do NOT repeat or paraphrase it):\n"
        "Title: {title}\n\n{text}\n\n--- END OF PARENT STORY ---\n\n"
        "SEQUEL REQUIREMENTS:\n"
        "- Continue the same narrator, setting, and unresolved threads.\n"
        "- Open with 1-2 sentences of light re-grounding ('A week after the incident…').\n"
        "- Escalate the threat. {ending_instruction}\n"
        "- story_title hint: short evocative sequel title (≤8 words, e.g. 'The Signal — Part 2')."
    ),
    "prequel": (
        "TASK: Write a PREQUEL to the r/nosleep story below, in {lang}.\n\n"
        "CHILD STORY (read-only context — do NOT repeat or paraphrase it):\n"
        "Title: {title}\n\n{text}\n\n--- END OF CHILD STORY ---\n\n"
        "PREQUEL REQUIREMENTS:\n"
        "- Set the events BEFORE the child story. Explain the origin of the threat or mystery.\n"
        "- The narrator may be the same person at an earlier time, or a different witness.\n"
        "- {ending_instruction}\n"
        "- story_title hint: prequel title (≤8 words, e.g. 'Before the Signal')."
    ),
    "expansion": (
        "TASK: Write an EXPANSION of the r/nosleep story below, in {lang}.\n\n"
        "ORIGINAL STORY (read-only context):\n"
        "Title: {title}\n\n{text}\n\n--- END OF ORIGINAL STORY ---\n\n"
        "EXPANSION REQUIREMENTS:\n"
        "- Add depth through new scenes, extended moments, or a secondary character's POV.\n"
        "- Do NOT retell the original — add what was left off-screen or implied.\n"
        "- {ending_instruction}\n"
        "- story_title hint: companion title (≤8 words, e.g. 'The Signal — Untold')."
    ),
}

_ENDING_INSTRUCTIONS: dict[str, dict[bool, str]] = {
    "sequel": {
        False: "End with a concrete resolution — the horror pays off.",
        True:  "Do NOT resolve the threat — end the last part on a hard cliffhanger that demands another sequel. Leave the horror undefeated and the narrator's fate uncertain.",
    },
    "prequel": {
        False: "The final scene must connect naturally to the opening of the child story.",
        True:  "End on an ominous, unresolved note — the origin of the threat is revealed but NOT contained, leaving the horror to spread forward in time.",
    },
    "expansion": {
        False: "The expansion stands alone as a companion piece — wrap up its own thread.",
        True:  "End the expansion on a hard unresolved cliffhanger — do NOT wrap up the new thread, leave the reader demanding more.",
    },
}


async def generate_followup(
    parent_title: str,
    parent_text: str,
    lang: str = "en",
    mode: str = "sequel",
    open_ending: bool = False,
) -> tuple[str | None, list[dict]]:
    """
    Generate a follow-up to an existing story and split it into video parts.

    mode:
      'sequel'    — continues after the story ends (default)
      'prequel'   — set before the story, explains origin of the threat
      'expansion' — adds off-screen depth without being a direct continuation

    Returns:
        (title, parts) — same shape as generate_and_split()
    """
    if mode not in _FOLLOWUP_MODE_PROMPTS:
        raise ValueError(f"Unknown followup mode: {mode!r}. Choose from {list(_FOLLOWUP_MODE_PROMPTS)}.")

    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, pdf_file = await loop.run_in_executor(None, _resolve_model)

    mode_block = _FOLLOWUP_MODE_PROMPTS[mode].format(
        lang=lang.upper(),
        title=parent_title,
        text=parent_text,
        ending_instruction=_ENDING_INSTRUCTIONS[mode][open_ending],
    )

    task_prompt = (
        f"{mode_block}\n\n"
        f"{_make_story_seed()}\n\n"
        "SPLITTING RULES:\n"
        "- Each part: exactly 230 to 450 words (strictly enforced for 90-180s TTS audio)\n"
        "- All parts EXCEPT the last MUST end mid-tension on a genuine cliffhanger\n"
        + (
            "- The LAST part MUST end on a hard unresolved cliffhanger — NO resolution, tease a sequel\n"
            if open_ending else
            "- The LAST part MUST end with a concrete resolution — the horror pays off\n"
        )
        + "- cliffhanger_note: \u226415 words on the tension (write 'RESOLUTION' for the last part if resolved)\n"
        "- Parts must be contiguous — no overlap, no gaps, no paraphrasing\n"
        "- part_number starts at 1\n"
        "- word_count is the exact word count of text_snippet\n"
        "- story_title: ONLY on part_number=1\n\n"
        "Return ONLY a JSON array — no prose, no markdown fences:\n"
        '[{"part_number": 1, "story_title": "...", "text_snippet": "...", "word_count": 250, "cliffhanger_note": "..."}, ...]'
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    parts = _parse_json_parts(response.text)
    title = _extract_title(parts)
    return title, parts


async def translate_text(text: str, target_lang: str) -> str:
    """Translate text to target_lang using a plain (uncached) model call."""
    if target_lang == "en":
        return text

    client = _get_client()
    settings = get_settings()
    primary = settings.gemini_model
    
    # Use standard resolve logic for consistency
    _, _, fallbacks, _ = _resolve_model()

    prompt = (
        f"Translate the following story text to {target_lang}. "
        "Preserve all paragraph breaks, punctuation, and narrative tone exactly. "
        "Return ONLY the translated text — no explanations, no commentary.\n\n"
        f"{text}"
    )
    response = await _generate_with_fallback(
        client, primary, fallbacks, [prompt],
        types.GenerateContentConfig(),
    )
    return response.text.strip()


async def rewrite_story(title: str, text: str) -> tuple[str, str]:
    """
    Rewrite a scraped Reddit story to bypass copyright/duplication checks and add flavor.
    Changes dates, names, locations, and wording while keeping the narrative identical.
    """
    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, pdf_file = await loop.run_in_executor(None, _resolve_model)

    task_prompt = (
        f"TASK: Rewrite the following horror story slightly to make it unique.\n\n"
        "REWRITE RULES:\n"
        "- Change specific details: names of people, towns, dates, and minor objects.\n"
        "- Rephrase descriptions and dialogue, but DO NOT SUMMARIZE. You MUST keep the exact same plot, pacing, and emotional beats.\n"
        "- CRITICAL: The rewritten story MUST be at least as long as the original text. Do NOT cut out any scenes, dialogue, or inner monologues. Rewrite it paragraph by paragraph.\n"
        "- Return ONLY a JSON object with 'title' (a new title) and 'text' (the rewritten story text).\n"
        "- Do NOT include any markdown, just raw JSON.\n\n"
        f"ORIGINAL TITLE: {title}\n"
        f"ORIGINAL TEXT:\n{text}\n\n"
        'Return format:\n{"title": "New Title", "text": "New story text..."}'
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            valid_json = raw[:e.pos].strip()
            try:
                data = json.loads(valid_json)
                logger.warning("Recovered from JSON 'Extra data' error in rewrite_story by stripping trailing text.")
            except Exception:
                pass
        
        # If still not parsed or it's an unescaped quote error
        if 'data' not in locals():
            logger.warning("JSONDecodeError in rewrite_story. Attempting regex fallback for unescaped quotes.")
            import re
            title_match = re.search(r'"title"\s* : \s*"((?:[^"\\]|\\.)*?)"\s*,', raw)
            text_match = re.search(r'"text"\s* : \s*"(.*)"\s*}', raw, re.DOTALL)
            if title_match and text_match:
                data = {"title": title_match.group(1), "text": text_match.group(1).replace('\n', '\\n')}
                logger.info("Successfully recovered using regex fallback.")
            else:
                logger.error(f"Failed to parse JSON in rewrite_story. Raw content was:\n{raw}\n---")
                raise e

    return data["title"], data["text"]


async def generate_title_only(text: str) -> str:
    """
    Generate a new title for a story without rewriting the text.
    Used for multi-part series to maintain text continuity while having a unique title.
    """
    loop = asyncio.get_event_loop()
    client, model_name, fallback_models, pdf_file = await loop.run_in_executor(None, _resolve_model)

    task_prompt = (
        f"TASK: Generate a completely new, creative, and unsettling title for this horror story.\n\n"
        "RULES:\n"
        "- Do NOT use the words 'Part' or any numbers.\n"
        "- Return ONLY the title as plain text. No markdown, no quotes.\n\n"
        f"STORY TEXT:\n{text[:2000]}..."
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="text/plain",
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    return response.text.strip()
