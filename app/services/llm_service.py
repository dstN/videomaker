"""
LLM Service — Gemini 3.1 Flash via the official google-genai SDK (2026).

Architecture:
  - Single `genai.Client` singleton per process
  - PDF uploaded ONCE to the File API (in-process TTL: 47h), reused across calls
  - No server-side CachedContent (not available on Free Tier)
  - Each generation call passes [pdf_file, task_prompt] directly in contents
  - system_instruction sent per-call via GenerateContentConfig
  - Structured output via response_schema — no hand-rolled JSON repair
  - Falls back gracefully to a plain model if the PDF is not on disk

Splitting never lets the model rewrite text: the story is cut into numbered
sentences locally, Gemini only picks the sentence where each part ends, and
the snippets are sliced from the original string. If the model's plan is
unusable, a deterministic local splitter takes over.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

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

# Temperature for creative story generation (high for variety; the response
# schema keeps the JSON structure valid).
_CREATIVE_TEMPERATURE = 1.4
# Splitting is an analytical task — deterministic output is what we want.
_SPLIT_TEMPERATURE = 0.0

# Target length for freshly generated stories (words).
_STORY_LENGTH_HINT = "1200 to 2000 words"

# ── Story seed injection ──────────────────────────────────────────────────────
# Random anchors injected per-call so every generation has a different starting
# point, preventing the model from falling into the same statistical grooves.
# No night-time 2-4 AM entries: the system instruction forbids witching-hour times.

_SEED_TIMES = [
    "6:14 AM", "11:38 PM", "4:52 PM", "8:07 AM", "1:23 PM", "9:41 PM",
    "7:30 PM", "10:18 AM", "5:03 PM", "12:47 PM", "5:56 AM", "10:02 PM",
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


# ── Response schemas ──────────────────────────────────────────────────────────

class _StoryDraft(BaseModel):
    story_title: str
    text: str


class _SplitBoundary(BaseModel):
    end_sentence: int
    cliffhanger_note: str


class _SplitPlan(BaseModel):
    story_title: str
    parts: list[_SplitBoundary]


class _Rewrite(BaseModel):
    title: str
    text: str


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
    Blocking (File API network calls) — run it in an executor from async code.
    """
    client = _get_client()
    settings = get_settings()

    # Fallback chain: Primary -> Flash -> configured fallback (Gemma 4 by default)
    fallbacks: list[str] = []
    for name in ("gemini-2.5-flash", settings.gemini_fallback_model):
        if name and name != settings.gemini_model and name not in fallbacks:
            fallbacks.append(name)

    pdf_file = _get_pdf_file(client)
    if pdf_file is None:
        logger.warning("No PDF available — proceeding without anti-AI PDF constraint.")
    return client, settings.gemini_model, fallbacks, pdf_file


async def _resolve_model_async() -> tuple[genai.Client, str, list[str], Any | None]:
    return await asyncio.get_running_loop().run_in_executor(None, _resolve_model)


def _is_retryable(exc: Exception) -> bool:
    """True for rate limits (429) and transient server errors (500/502/503/504)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).upper()
    return any(
        marker in msg
        for marker in ("429", "RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED", "UNAVAILABLE", "OVERLOADED", "503")
    )


async def _generate_with_fallback(
    client: genai.Client,
    primary_model: str,
    fallback_models: list[str],
    contents: list,
    config: types.GenerateContentConfig,
) -> Any:
    """
    Call generate_content with a chain of fallbacks on rate-limit / transient errors.
    """
    models_to_try = [primary_model] + fallback_models

    for i, model_name in enumerate(models_to_try):
        try:
            current_contents = contents
            current_config = config
            if "gemma" in model_name.lower():
                # Gemma doesn't take File API inputs or a response schema
                text_only = [c for c in contents if isinstance(c, str)]
                current_contents = text_only if text_only else contents
                current_config = config.model_copy(update={"response_schema": None})

            return await client.aio.models.generate_content(
                model=model_name,
                contents=current_contents,
                config=current_config,
            )
        except Exception as exc:
            if not _is_retryable(exc) or i == len(models_to_try) - 1:
                raise

            logger.warning(
                "Model %s failed (%s) — retrying with next fallback %s",
                model_name,
                str(exc)[:120],
                models_to_try[i + 1],
            )


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _parse_model(response: Any, model_cls: type[_ModelT]) -> _ModelT:
    """Use the SDK's parsed object when present, else validate the raw text."""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, model_cls):
        return parsed
    return model_cls.model_validate_json(_strip_fences(response.text))


# ── Sentence segmentation & local splitting ──────────────────────────────────

# A sentence ends after terminal punctuation (plus closing quotes/brackets)
# followed by whitespace, or at a line break.
_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+[\"”’'\)\]]*(?=\s)|\n")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) character spans of the sentences in text.
    Spans cover every non-whitespace character exactly once, in order.
    """
    spans: list[tuple[int, int]] = []

    def _add(start: int, end: int) -> None:
        chunk = text[start:end]
        stripped = chunk.strip()
        if stripped:
            offset = start + (len(chunk) - len(chunk.lstrip()))
            spans.append((offset, offset + len(stripped)))

    start = 0
    for m in _SENTENCE_BOUNDARY.finditer(text):
        _add(start, m.end())
        start = m.end()
    _add(start, len(text))
    return spans


def plan_parts_locally(
    word_counts: list[int],
    min_words: int,
    max_words: int,
    max_parts: int | None = None,
) -> list[int]:
    """
    Choose part boundaries (exclusive end sentence indices) so each part lands
    inside [min_words, max_words], close to the upper end of the range
    (fewer parts per story), using at most max_parts parts.

    The part limit wins over the word range: a story too long for
    max_parts × max_words gets evenly stretched parts instead of extra ones.
    Dynamic programming over (parts used, sentence boundary).
    """
    n = len(word_counts)
    if n == 0:
        return []
    prefix = [0]
    for wc in word_counts:
        prefix.append(prefix[-1] + wc)
    total = prefix[-1]
    if total <= max_words:
        return [n]

    # More parts than this never lower the cost (each would fall below
    # min_words); bounding k keeps the DP at O(k · n · span) instead of O(n³).
    useful = math.ceil(total / min_words) + 2
    limit = min(n, max_parts or useful, useful)
    # Aim near the upper end: fewer, longer parts (max_words is still hard).
    target = min_words + 0.85 * (max_words - min_words)

    def cost(words: int) -> float:
        if min_words <= words <= max_words:
            return (words - target) ** 2
        outside = min_words - words if words < min_words else words - max_words
        return 1e6 + (outside ** 2) * 100

    inf = float("inf")
    # best[k][j]: minimal cost to cover sentences [0, j) with exactly k parts
    best = [[inf] * (n + 1) for _ in range(limit + 1)]
    prev = [[0] * (n + 1) for _ in range(limit + 1)]
    best[0][0] = 0.0
    # A part never needs more than the evenly stretched worst case plus slack
    span_cap = max(2 * max_words, 2 * total / limit)
    for k in range(1, limit + 1):
        for j in range(1, n + 1):
            for i in range(j - 1, k - 2, -1):
                words = prefix[j] - prefix[i]
                if words > span_cap and j - i > 1:
                    break
                if best[k - 1][i] == inf:
                    continue
                c = best[k - 1][i] + cost(words)
                if c < best[k][j]:
                    best[k][j] = c
                    prev[k][j] = i

    k = min(range(1, limit + 1), key=lambda kk: best[kk][n])
    ends: list[int] = []
    j = n
    while k > 0:
        ends.append(j)
        j = prev[k][j]
        k -= 1
    return list(reversed(ends))


def _plan_is_usable(
    ends: list[int],
    word_counts: list[int],
    min_words: int,
    max_words: int,
    max_parts: int | None = None,
) -> bool:
    """
    Validate an LLM split plan: contiguous, complete, part count and word range.
    max_words is a hard limit (it keeps videos under the Shorts length);
    a single sentence longer than max_words is tolerated as its own part.
    """
    n = len(word_counts)
    if not ends or ends[-1] != n or any(b <= a for a, b in zip([0] + ends, ends)):
        return False
    if max_parts and len(ends) > max_parts:
        return False
    lo = int(min_words * 0.85)
    start = 0
    for idx, end in enumerate(ends):
        words = sum(word_counts[start:end])
        is_last = idx == len(ends) - 1
        if words > max_words and end - start > 1:
            return False
        if words < lo and not (is_last and words >= min_words * 0.5):
            return False
        start = end
    return True


def _build_parts(
    text: str,
    spans: list[tuple[int, int]],
    ends: list[int],
    notes: list[str],
) -> list[dict]:
    parts: list[dict] = []
    start = 0
    for idx, end in enumerate(ends):
        snippet = text[spans[start][0]:spans[end - 1][1]]
        parts.append({
            "part_number": idx + 1,
            "text_snippet": snippet,
            "word_count": len(snippet.split()),
            "cliffhanger_note": notes[idx] if idx < len(notes) else "",
        })
        start = end
    return parts


# ── Public API ────────────────────────────────────────────────────────────────

async def split_into_parts(
    text: str,
    lang: str = "en",
    open_ending: bool = False,
) -> tuple[str | None, list[dict]]:
    """
    Split story text into video parts of PART_MIN_WORDS..PART_MAX_WORDS words
    (max is hard: it keeps every video under MAX_PART_SECONDS). There is no
    limit on the number of parts here — store_parts() turns stories with
    more than MAX_PARTS parts into a series.
    Snippets are sliced verbatim from the input — the model only chooses cut points.

    open_ending=True  → last part's note describes the unresolved cliffhanger.
    open_ending=False → last part's note is 'RESOLUTION'.

    Returns:
        (story_title, parts) where parts is a list of
        [{part_number, text_snippet, word_count, cliffhanger_note}]
    """
    settings = get_settings()
    min_w, max_w = settings.part_min_words, settings.part_max_words

    spans = split_sentences(text)
    if not spans:
        raise ValueError("Cannot split empty story text.")
    sentences = [text[s:e] for s, e in spans]
    word_counts = [len(s.split()) for s in sentences]
    n = len(sentences)

    last_note_rule = (
        "≤15 words describing the unresolved dread (NOT 'RESOLUTION')"
        if open_ending else
        "write 'RESOLUTION'"
    )
    numbered = "\n".join(
        f"[{i}] ({wc}w) {s.replace(chr(10), ' ')}"
        for i, (s, wc) in enumerate(zip(sentences, word_counts), 1)
    )
    task_prompt = (
        f"TASK: The story below ({lang.upper()}) is given as {n} numbered sentences, "
        "each with its word count. Decide where each short-video part ends.\n\n"
        "RULES:\n"
        f"- Each part must contain {min_w} to {max_w} words in total (sum the word counts). "
        f"{max_w} is a hard maximum.\n"
        "- Every part except the last must end on a sentence that leaves the viewer in suspense.\n"
        "- Parts are contiguous. For each part return end_sentence = the number of its LAST sentence.\n"
        f"- end_sentence values strictly increase; the final part MUST end at sentence {n}.\n"
        "- cliffhanger_note: ≤15 words on the tension at the cut; "
        f"for the last part {last_note_rule}.\n"
        "- story_title: a short evocative title (≤6 words) for the full story.\n\n"
        f"SENTENCES:\n{numbered}"
    )

    title: str | None = None
    ends: list[int] = []
    notes: list[str] = []
    try:
        client, model_name, fallback_models, _ = await _resolve_model_async()
        gen_config = types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=_SplitPlan,
            temperature=_SPLIT_TEMPERATURE,
        )
        response = await _generate_with_fallback(
            client, model_name, fallback_models, [task_prompt], gen_config
        )
        plan = _parse_model(response, _SplitPlan)
        title = plan.story_title.strip() or None
        ends = [b.end_sentence for b in plan.parts]
        notes = [b.cliffhanger_note for b in plan.parts]
    except Exception as exc:
        logger.warning("LLM split plan failed (%s) — using local splitter.", str(exc)[:200])

    if not _plan_is_usable(ends, word_counts, min_w, max_w):
        if ends:
            logger.warning(
                "LLM split plan rejected (ends=%s, %d sentences) — using local splitter.",
                ends, n,
            )
        ends = plan_parts_locally(word_counts, min_w, max_w)
        notes = []
        if not open_ending:
            notes = [""] * (len(ends) - 1) + ["RESOLUTION"]

    return title, _build_parts(text, spans, ends, notes)


async def _generate_story_text(task_prompt: str) -> tuple[str, str]:
    """Run a creative generation prompt and return (title, full story text)."""
    client, model_name, fallback_models, pdf_file = await _resolve_model_async()
    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])
    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=_StoryDraft,
        temperature=_CREATIVE_TEMPERATURE,
    )
    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    draft = _parse_model(response, _StoryDraft)
    return draft.story_title.strip(), draft.text.strip()


async def generate_and_split(
    prompt: str,
    lang: str = "en",
    open_ending: bool = False,
) -> tuple[str | None, list[dict]]:
    """
    Generate a new r/nosleep style story from a user prompt, then split it
    into video parts (see split_into_parts).

    open_ending=True  → the story ends on a cliffhanger (teases a sequel).
    open_ending=False → the story resolves the central threat (default).

    Returns:
        (story_title, parts) where parts is a list of
        [{part_number, text_snippet, word_count, cliffhanger_note}]
    """
    if open_ending:
        ending_rule = (
            "- The story does NOT need to resolve — it must end on a HARD, irresistible cliffhanger.\n"
            "  The final scene should cut off at the worst possible moment. Leave the central threat\n"
            "  unanswered. The viewer MUST feel compelled to watch the next story."
        )
    else:
        ending_rule = (
            "- The story MUST be complete with a real, concrete ending.\n"
            "- The final scene must resolve the central threat or mystery — not leave it dangling.\n"
            "  'I heard her voice' or 'I still don't know what it was' are NOT endings. Go further."
        )

    task_prompt = (
        f"TASK: Write an original Reddit r/nosleep horror story in {lang.upper()} "
        f"based on this premise:\n\n\"{prompt}\"\n\n"
        f"{_make_story_seed()}\n\n"
        f"STORY REQUIREMENTS:\n{ending_rule}\n"
        f"- Length: {_STORY_LENGTH_HINT}.\n"
        "- The story will be cut into 1-3 minute video parts, so build tension in waves:\n"
        "  a new unsettling beat roughly every 250-400 words.\n"
        "- story_title: a short evocative title (≤6 words).\n\n"
        "Return the full story as plain prose in 'text' (paragraphs separated by blank lines)."
    )

    title, text = await _generate_story_text(task_prompt)
    split_title, parts = await split_into_parts(text, lang, open_ending=open_ending)
    return title or split_title, parts


async def generate_description(title: str, full_text: str, lang: str = "en") -> str:
    """
    Generate a YouTube/TikTok-style video description + 5 hashtags for a story.
    #nosleep is always included.  Returns a ready-to-paste plain-text string.
    """
    client, model_name, fallback_models, _ = await _resolve_model_async()

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
        "- story_title: short evocative sequel title (≤8 words, e.g. 'The Signal — Part 2')."
    ),
    "prequel": (
        "TASK: Write a PREQUEL to the r/nosleep story below, in {lang}.\n\n"
        "CHILD STORY (read-only context — do NOT repeat or paraphrase it):\n"
        "Title: {title}\n\n{text}\n\n--- END OF CHILD STORY ---\n\n"
        "PREQUEL REQUIREMENTS:\n"
        "- Set the events BEFORE the child story. Explain the origin of the threat or mystery.\n"
        "- The narrator may be the same person at an earlier time, or a different witness.\n"
        "- {ending_instruction}\n"
        "- story_title: prequel title (≤8 words, e.g. 'Before the Signal')."
    ),
    "expansion": (
        "TASK: Write an EXPANSION of the r/nosleep story below, in {lang}.\n\n"
        "ORIGINAL STORY (read-only context):\n"
        "Title: {title}\n\n{text}\n\n--- END OF ORIGINAL STORY ---\n\n"
        "EXPANSION REQUIREMENTS:\n"
        "- Add depth through new scenes, extended moments, or a secondary character's POV.\n"
        "- Do NOT retell the original — add what was left off-screen or implied.\n"
        "- {ending_instruction}\n"
        "- story_title: companion title (≤8 words, e.g. 'The Signal — Untold')."
    ),
}

_ENDING_INSTRUCTIONS: dict[str, dict[bool, str]] = {
    "sequel": {
        False: "End with a concrete resolution — the horror pays off.",
        True:  "Do NOT resolve the threat — end on a hard cliffhanger that demands another sequel. Leave the horror undefeated and the narrator's fate uncertain.",
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

    mode_block = _FOLLOWUP_MODE_PROMPTS[mode].format(
        lang=lang.upper(),
        title=parent_title,
        text=parent_text,
        ending_instruction=_ENDING_INSTRUCTIONS[mode][open_ending],
    )

    task_prompt = (
        f"{mode_block}\n\n"
        f"{_make_story_seed()}\n\n"
        f"- Length: {_STORY_LENGTH_HINT}.\n"
        "- The story will be cut into 1-3 minute video parts, so build tension in waves:\n"
        "  a new unsettling beat roughly every 250-400 words.\n\n"
        "Return the full story as plain prose in 'text' (paragraphs separated by blank lines)."
    )

    title, text = await _generate_story_text(task_prompt)
    split_title, parts = await split_into_parts(text, lang, open_ending=open_ending)
    return title or split_title, parts


async def translate_text(text: str, target_lang: str, source_lang: str = "en") -> str:
    """Translate text to target_lang using a plain (uncached) model call."""
    if target_lang == source_lang:
        return text

    client, primary, fallbacks, _ = await _resolve_model_async()

    prompt = (
        f"Translate the following story text from {source_lang} to {target_lang}. "
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
    client, model_name, fallback_models, pdf_file = await _resolve_model_async()

    task_prompt = (
        f"TASK: Rewrite the following horror story slightly to make it unique.\n\n"
        "REWRITE RULES:\n"
        "- Change specific details: names of people, towns, dates, and minor objects.\n"
        "- Rephrase descriptions and dialogue, but DO NOT SUMMARIZE. You MUST keep the exact same plot, pacing, and emotional beats.\n"
        "- CRITICAL: The rewritten story MUST be at least as long as the original text. Do NOT cut out any scenes, dialogue, or inner monologues. Rewrite it paragraph by paragraph.\n"
        "- Return 'title' (a new title) and 'text' (the rewritten story text).\n\n"
        f"ORIGINAL TITLE: {title}\n"
        f"ORIGINAL TEXT:\n{text}"
    )

    contents: list = ([pdf_file, task_prompt] if pdf_file is not None else [task_prompt])

    gen_config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=_Rewrite,
        temperature=_CREATIVE_TEMPERATURE,
    )

    response = await _generate_with_fallback(client, model_name, fallback_models, contents, gen_config)
    data = _parse_model(response, _Rewrite)
    return data.title, data.text


async def generate_title_only(text: str) -> str:
    """
    Generate a new title for a story without rewriting the text.
    Used for multi-part series to maintain text continuity while having a unique title.
    """
    client, model_name, fallback_models, pdf_file = await _resolve_model_async()

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
