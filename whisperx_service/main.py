"""
WhisperX microservice — runs as a standalone FastAPI inside the CUDA container.
Accepts POST /transcribe with {"audio_path": "/output/..."} and returns
word-level timestamp JSON consumed by the main API.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import whisperx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhisperX Transcription Service", version="1.0.0")

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


@app.on_event("startup")
async def load_model() -> None:
    global _model, _align_model, _align_metadata
    logger.info("Loading WhisperX model '%s' on %s...", MODEL_SIZE, DEVICE)
    _model = whisperx.load_model(
        MODEL_SIZE,
        DEVICE,
        compute_type=COMPUTE_TYPE,
        asr_options={"suppress_numerals": True},
    )
    logger.info("WhisperX model loaded.")


# ── Request / Response ────────────────────────────────────────────────────────

class TranscribeRequest(BaseModel):
    audio_path: str
    language: str = "en"


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float


class TranscribeResponse(BaseModel):
    words: list[WordTimestamp]


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(request: TranscribeRequest) -> TranscribeResponse:
    audio_path = Path(request.audio_path)
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"Audio file not found: {audio_path}")

    global _model, _align_model, _align_metadata

    try:
        # Load audio
        audio = whisperx.load_audio(str(audio_path))

        # Transcribe
        result = _model.transcribe(audio, batch_size=16, language=request.language)

        # Load alignment model lazily per language
        if _align_model is None or getattr(_align_metadata, "language", None) != request.language:
            _align_model, _align_metadata = whisperx.load_align_model(
                language_code=request.language,
                device=DEVICE,
            )

        # Align to get word-level timestamps
        aligned = whisperx.align(
            result["segments"],
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

        return TranscribeResponse(words=words)

    except Exception as exc:
        logger.exception("Transcription failed for %s", audio_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}
