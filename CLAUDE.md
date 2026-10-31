# CLAUDE.md - Project Guidelines & Architecture (Updated 2026)

## 1. Project Overview
**Name:** Infinity Mystery Video Factory 
**Purpose:** A local, Dockerized (WSL2) video automation pipeline. It generates originial Reddit r/nosleep style stories (or translates existing ones), splits them semantically into 60-180 second parts (hard 180 s limit for YouTube Shorts, max. 6 per story; longer stories become a series) with cliffhangers via Gemini API, generates local TTS, syncs subtitles, and renders final vertical videos with background assets.
**Key Constraint:** Multi-language scalability (EN & DE), zero OpenAI APIs, and a hybrid hardware allocation that fits an RTX 4060 (8 GB VRAM).

---

## 2. Tech Stack (Target: 2026 Standards)
- **Environment:** WSL2 (Ubuntu) via Docker Compose with NVIDIA GPU Runtime (`deploy.resources.reservations.devices`).
- **Backend / Orchestrator:** Python 3.12+ with FastAPI (Async orchestration).
- **Frontend:** Streamlit (Local Web UI running on port 8501).
- **Database:** SQLite using SQLAlchemy for relational tracking. Foreign keys are enforced per connection; `init_db()` adds missing columns on startup (forward-only).
- **AI Brain (Logic, Writing & Splitting):** `google-genai` (Gemini 3.1 Flash API - **MUST use the new 2026 SDK**).
- **Voice (TTS):** `TrevorJS/voxtral-tts-q4-gguf` (Local Mistral Model, 4-bit GGUF via Hugging Face), run by the `voxtral-mini-realtime-rs` binary.
- **Transcription/Sync:** `whisperx` (Dockerized, local CUDA).
- **Video Engine:** `ffmpeg` called directly (ASS subtitles, filter_complex). MUST use hardware acceleration (`h264_nvenc`); falls back to `libx264` only if NVENC is unavailable.

---

## 3. Architectural Directives

### 3.1. Database Schema
- `Story`: `id`, `title`, `original_text_en` (source text — written in `source_language`, despite the name), `source_language`, `source_url`, `voice`, `parent_story_id`, `followup_mode`, `open_ending`, `created_at`.
- `Translation`: `id`, `story_id`, `language_code`, `translated_text` (unique per story + language).
- `VideoPart`: `id`, `translation_id`, `part_number`, `text_snippet`, `cliffhanger_note`, `audio_path`, `timestamps_path`.
- `RenderJob`: `id`, `video_part_id`, `status` (pending, audio_done, sync_done, rendering, completed, failed), `file_path`, `error_message`, `updated_at`.

### 3.2. Core Pipeline Flow & Hardware Allocation
1. **Ingestion:** UI accepts a prompt for a new story or a raw text. Parts approved in the preview are stored as-is.
2. **Generation/Splitting (CLOUD):** Gemini 3.1 Flash generates/translates the text. Splitting into parts of **190-460 words** (60-180s audio at 2.65-3.2 words/s, `PART_MIN_WORDS`/`PART_MAX_WORDS`; every video MUST stay ≤ 180 s, `MAX_PART_SECONDS`) works on numbered sentences: Gemini only returns where each part ends (JSON via `response_schema`), the snippets are sliced verbatim from the source text, and a deterministic local splitter is used if the plan is invalid. Text must never be lost or paraphrased during splitting. A story with more than 6 parts (`MAX_PARTS`) becomes a series of linked stories (`followup_mode='continuation'`, "<title> – Part N"), front-loaded: 7 → 4/3, 8 → 4/4, 9 → 5/4, 13 → 5/4/4.
   * *Critical Anti-AI-Sign Rule:* Gemini MUST receive `assets/pdf/signs_of_ai_writing.pdf` as a negative constraint. It is strictly forbidden to use words like *crucial, pivotal, significant, testament, tapestry, vibrant, landscape, underscore*. Prohibited patterns: Negative parallelisms ("Not only X, but also Y"), Rule of Three (3 adjectives in a row), and ending sections with formulaic summaries.
   * The PDF is uploaded via the File API and reused for 47h. Explicit context caching (`client.caches.create`) is not used because it is unavailable on the Free Tier.
3. **Audio Generation (Windows host GPU):** Voxtral runs on the Windows host via Vulkan (`voxtral_windows_bin/Start-VoxtralService.ps1`, port 8700). Vulkan is not usable inside WSL2 Docker; the CPU build (`Dockerfile.voxtral`) is kept as an alternative. Voices are Voxtral presets (`casual_female`, `casual_male`, `neutral_female`, `neutral_male` are used); the binary's README documents only preset voices (20 across 9 languages), no zero-shot cloning from `assets/voice/clone_sample.wav`. Numbers are spelled out in the story's language before synthesis. If a part's audio would exceed the 180 s video limit it is sped up by at most 15 %; beyond that the job fails instead of producing a longer video.
4. **Timestamping (GPU):** `whisperx` force-aligns the audio against the known spoken text on the RTX 4060 (ASR only as fallback). Default `compute_type="float16"`; `int8_float16` can be selected via `WHISPERX_COMPUTE_TYPE` (fails on some CUDA 12 + cuDNN 8 setups).
5. **Rendering (GPU):** ffmpeg overlays word-by-word ASS subtitles (Courier Prime, outline) and localized "Part x / y" + follow CTA over a random background and music, exported via `h264_nvenc`.
6. **Export:** When all parts of a story are done the pipeline writes `.done`; `export_watcher.sh` (WSL host) copies the videos + `description.txt` to Google Drive and deletes the local folder. `RenderJob.file_path` therefore points to a deleted file afterwards.

---

## 4. Coding Standards & Rules for Claude

1. **Automated Setup:** The backend MUST automatically download required assets on first run:
   - Font `CourierPrime-Regular.ttf` from Google Fonts into `/assets/fonts/`.
   - The Voxtral GGUF model (`TrevorJS/voxtral-tts-q4-gguf`) via `huggingface_hub` into the local cache. No Auth-Token required.
   - `signs_of_ai_writing.pdf` is NOT downloaded — it must be placed in `assets/pdf/` manually.
2. **Docker & WSL2 Strictness:** Ensure the Docker Compose file explicitly maps the NVIDIA GPU. 
3. **API Setup (Gemini):** The `GEMINI_API_KEY` is loaded via `.env`. You MUST use the `google-genai` package (e.g., `client = genai.Client(...)`). Do NOT use the legacy `google.generativeai` package.
4. **Idempotency:** The pipeline must be recoverable. If rendering fails, avoid regenerating TTS. Jobs interrupted mid-render are reset on startup; never delete artefacts of completed/exported parts.
5. **Tests:** `pytest` (see `requirements-dev.txt`) runs offline without GPU or Gemini. Run it before committing.
6. **Directory Structure:**
    /project_root
    ├── /app                # FastAPI backend & logic
    ├── /ui                 # Streamlit frontend
    ├── /whisperx_service   # WhisperX CUDA microservice
    ├── /voxtral_service    # HTTP wrapper around the voxtral binary
    ├── /voxtral_windows_bin # Windows binary + start scripts (binaries not in git)
    ├── /tests              # Offline pytest suite
    ├── /assets
    │   ├── /pdf            # Contains signs_of_ai_writing.pdf
    │   ├── /voice          # Unused (no voice cloning, see 3.2)
    │   ├── /fonts          # Auto-downloaded ttf fonts
    │   ├── /backgrounds    
    │   └── /music          
    ├── /output             # Final rendered videos
    ├── /data               # SQLite database file, models
    ├── docker-compose.yml
    └── requirements.txt
