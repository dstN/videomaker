"""
WhisperX microservice — runs as a standalone FastAPI inside the CUDA container.
Accepts POST /transcribe with {"audio_path": "/output/...", "text": "..."} and
returns word-level timestamp JSON consumed by the main API.

When the spoken text is known (it always is for TTS output) the audio is
force-aligned against it, so subtitles show the script verbatim instead of
ASR guesses. Without text, or if alignment covers too few words, it falls
back to transcription + alignment.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
import whisperx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Model singleton — loaded once at startup ──────────────────────────────────
_model: Any = None
_align_model: Any = None
_align_metadata: Any = None
DEVICE = "cuda"
# float16: universell unterstuetzt auf NVIDIA Ampere (RTX 4060).
# int8_float16 kann auf manchen CUDA 12 + cuDNN 8 Setups fehlschlagen.
# Ueber env var ueberschreibbar: WHISPERX_COMPUTE_TYPE=int8_float16
COMPUTE_TYPE = os.getenv("WHISPERX_COMPUTE_TYPE", "float16")
MODEL_SIZE = os.getenv("WHISPERX_MODEL", "base")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # Optional, for diarization
SAMPLE_RATE = 16_000  # whisperx.load_audio resamples to 16 kHz
# Forced alignment must place at least this share of the script's words,
# otherwise the transcription path is used instead.
MIN_ALIGNED_RATIO = 0.6


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _model
    logger.info("Loading WhisperX model '%s' on %s...", MODEL_SIZE, DEVICE)
    _model = whisperx.load_model(
        MODEL_SIZE,
        DEVICE,
        compute_type=COMPUTE_TYPE,
        asr_options={"suppress_numerals": True},
    )
    logger.info("WhisperX model loaded.")
    yield


app = FastAPI(title="WhisperX Transcription Service", version="1.1.0", lifespan=lifespan)


# ── Request / Response ────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    audio_path: str
    language: str = "en"
    text: str | None = None  # known script → forced alignment instead of ASR


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float


class TranscribeResponse(BaseModel):
    words: list[WordTimestamp]


# ── Endpoint ──────────────────────────────────────────────────────────────────

def _ensure_align_model(language: str) -> None:
    global _align_model, _align_metadata
    if _align_model is None or _align_metadata.get("language") != language:
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language,
            device=DEVICE,
        )


def _align(segments: list[dict], audio: Any) -> list[WordTimestamp]:
    aligned = whisperx.align(
        segments,
        _align_model,
        _align_metadata,
        audio,
        DEVICE,
        return_char_alignments=False,
    )
    words: list[WordTimestamp] = []
    for segment in aligned.get("segments", []):
        for w in segment.get("words", []):
            words.append(
                WordTimestamp(
                    word=w.get("word", "").strip(),
                    start=round(w.get("start", 0.0), 3),
                    end=round(w.get("end", 0.0), 3),
                )
            )
    return words


# Sync endpoint: FastAPI runs it in a worker thread, so the GPU work does
# not block the event loop (health checks stay responsive).
@app.post("/transcribe", response_model=TranscribeResponse)
def transcribe(request: TranscribeRequest) -> TranscribeResponse:
    audio_path = Path(request.audio_path)
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"Audio file not found: {audio_path}")

    try:
        audio = whisperx.load_audio(str(audio_path))
        _ensure_align_model(request.language)

        if request.text and request.text.strip():
            duration = len(audio) / SAMPLE_RATE
            try:
                words = _align([{"text": request.text, "start": 0.0, "end": duration}], audio)
            except Exception:
                # e.g. CUDA OOM on a long single segment — ASR splits into short segments
                logger.exception("Forced alignment failed for %s — falling back to ASR.", audio_path.name)
                torch.cuda.empty_cache()
                words = []
            timed = sum(1 for w in words if w.end > 0)
            expected = len(request.text.split())
            if expected and timed / expected >= MIN_ALIGNED_RATIO:
                return TranscribeResponse(words=words)
            if words:
                logger.warning(
                    "Forced alignment placed only %d/%d words for %s — falling back to ASR.",
                    timed, expected, audio_path.name,
                )

        result = _model.transcribe(audio, batch_size=16, language=request.language)
        return TranscribeResponse(words=_align(result["segments"], audio))

    except Exception as exc:
        logger.exception("Transcription failed for %s", audio_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}
