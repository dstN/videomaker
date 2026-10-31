# CLAUDE.md - Project Guidelines & Architecture (Updated 2026)

## 1. Project Overview
**Name:** Infinity Mystery Video Factory 
**Purpose:** A local, Dockerized (WSL2) video automation pipeline. It generates originial Reddit r/nosleep style stories (or translates existing ones), splits them semantically into 60-90 second parts with cliffhangers via Gemini API, generates local TTS with voice cloning, syncs subtitles, and renders final vertical videos with background assets.
**Key Constraint:** Multi-language scalability (EN & DE), zero OpenAI APIs, and a strict hybrid hardware allocation (CPU for Audio Generation, GPU via WSL2 for Sync & Rendering).

---

## 2. Tech Stack (Target: 2026 Standards)
- **Environment:** WSL2 (Ubuntu) via Docker Compose with NVIDIA GPU Runtime (`deploy.resources.reservations.devices`).
- **Backend / Orchestrator:** Python 3.12+ with FastAPI (Async orchestration).
- **Frontend:** Streamlit (Local Web UI running on port 8501).
- **Database:** SQLite using SQLAlchemy (or SQLModel) for relational tracking.
- **AI Brain (Logic, Writing & Splitting):** `google-genai` (Gemini 3.1 Flash API - **MUST use the new 2026 SDK**).
- **Voice (TTS):** `TrevorJS/voxtral-tts-q4-gguf` (Local Mistral Model, 4-bit GGUF via Hugging Face).
- **Transcription/Sync:** `whisperx` (Dockerized, local CUDA).
- **Video Engine:** `moviepy` (v2.0+) acting as a wrapper for `ffmpeg`. MUST use hardware acceleration (`h264_nvenc`).

---

## 3. Architectural Directives

### 3.1. Database Schema
- `Story`: `id`, `original_prompt_or_text`, `language`, `created_at`.
- `VideoPart`: `id`, `story_id`, `part_number`, `text_snippet`, `word_count`, `visual_prompt_for_bg`.
- `RenderJob`: `id`, `video_part_id`, `status` (pending, audio_done, sync_done, rendering, completed), `file_path`.

### 3.2. Core Pipeline Flow & Hardware Allocation
1. **Ingestion:** UI accepts a prompt for a new story or a raw text.
2. **Generation/Splitting (CLOUD):** Gemini 3.1 Flash generates/translates the text into chunks of exactly **160-210 words** (60-90s audio). The output MUST be valid JSON.
   * *Critical Anti-AI-Sign Rule:* Gemini MUST use the cached file `assets/pdf/signs_of_ai_writing.pdf` as a negative constraint. It is strictly forbidden to use words like *crucial, pivotal, significant, testament, tapestry, vibrant, landscape, underscore*. Prohibited patterns: Negative parallelisms ("Not only X, but also Y"), Rule of Three (3 adjectives in a row), and ending sections with formulaic summaries.
3. **Audio Generation (CPU/RAM):** Voxtral-4B (GGUF) runs strictly on `device="cpu"` to save VRAM. It utilizes zero-shot voice cloning using `assets/voice/clone_sample.wav`.
4. **Timestamping (GPU):** `whisperx` analyzes the `.wav` on the RTX 4060. MUST use `compute_type="int8_float16"` to fit within 8GB VRAM limit.
5. **Rendering (GPU):** MoviePy overlays subtitles synchronously (CourierPrime font, subtle glow) over background assets and exports via `h264_nvenc`.

---

## 4. Coding Standards & Rules for Claude

1. **Automated Setup:** The backend MUST automatically download required assets on first run:
   - Font `CourierPrime-Regular.ttf` from Google Fonts into `/assets/fonts/`.
   - The Voxtral GGUF model (`TrevorJS/voxtral-tts-q4-gguf`) via `huggingface_hub` into the local cache. No Auth-Token required.
2. **Docker & WSL2 Strictness:** Ensure the Docker Compose file explicitly maps the NVIDIA GPU. 
3. **API Setup (Gemini):** The `GEMINI_API_KEY` is loaded via `.env`. You MUST use the `google-genai` package (e.g., `client = genai.Client(...)`) and its explicit Caching API (`client.caches.create`). Do NOT use the legacy `google.generativeai` package.
4. **Idempotency:** The pipeline must be recoverable. If rendering fails, avoid regenerating TTS.
5. **Directory Structure:**
    /project_root
    ├── /app                # FastAPI backend & logic
    ├── /ui                 # Streamlit frontend
    ├── /assets
    │   ├── /pdf            # Contains signs_of_ai_writing.pdf
    │   ├── /voice          # Contains clone_sample.wav
    │   ├── /fonts          # Auto-downloaded ttf fonts
    │   ├── /backgrounds    
    │   └── /music          
    ├── /output             # Final rendered videos
    ├── /data               # SQLite database file
    ├── docker-compose.yml
    └── requirements.txt