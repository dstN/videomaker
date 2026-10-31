"""
Voxtral TTS Microservice — lightweight HTTP wrapper around the voxtral CLI binary.

The voxtral-mini-realtime-rs project provides a CLI (`voxtral speak`) but no
built-in HTTP server. This wrapper bridges the gap, exposing a simple REST API
that the FastAPI backend can call via httpx.

Routes:
  POST /synthesize  {text, voice?, euler_steps?} → audio/wav
  GET  /health                                   → 200 JSON
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [voxtral] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voxtral TTS Engine", version="1.0.0")

# ── Config ─────────────────────────────────────────────────────────────────────
# VOXTRAL_BIN: auf Windows zeigt dies auf voxtral.exe (via VOXTRAL_BIN env-var
#              aus Start-VoxtralService.ps1), auf Linux auf /usr/local/bin/voxtral
VOXTRAL_BIN = os.getenv("VOXTRAL_BIN", "/usr/local/bin/voxtral")
GGUF_PATH = os.getenv("GGUF_PATH", "/data/models/voxtral/voxtral-tts-q4.gguf")
TOKENIZER_PATH = os.getenv("TOKENIZER_PATH", "/data/models/voxtral/tekken.json")
DEFAULT_VOICE = "casual_female"
DEFAULT_EULER_STEPS = int(os.getenv("DEFAULT_EULER_STEPS", "1"))
# MAX_FRAMES begrenzt den Decoder-Output-Buffer.
# Default (2000) fuehrt zu einer ~8GB cubecl-Pool-Allokation -> OOM auf RTX 4060.
# 300 Frames ≈ ~10s Audio pro Chunk; der Caller muss laengere Texte sowieso aufteilen.
MAX_FRAMES = int(os.getenv("VOXTRAL_MAX_FRAMES", "300"))
SYNTHESIS_TIMEOUT = 360  # seconds


# ── Schemas ────────────────────────────────────────────────────────────────────

class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    voice: str = Field(DEFAULT_VOICE)
    euler_steps: int = Field(DEFAULT_EULER_STEPS, ge=1, le=10)
    max_frames: int = Field(MAX_FRAMES, ge=50, le=2000)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    issues = []
    if not Path(VOXTRAL_BIN).exists():
        issues.append(f"binary not found: {VOXTRAL_BIN}")
    if not Path(GGUF_PATH).exists():
        issues.append(f"GGUF model not found: {GGUF_PATH}")
    if not Path(TOKENIZER_PATH).exists():
        issues.append(f"tokenizer not found: {TOKENIZER_PATH}")
    if issues:
        raise HTTPException(status_code=503, detail="; ".join(issues))
    return {"status": "ok", "gguf": GGUF_PATH, "binary": VOXTRAL_BIN}


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    if not Path(GGUF_PATH).exists():
        raise HTTPException(
            status_code=503,
            detail=f"GGUF model not found at {GGUF_PATH}. "
                   "Ensure the model is downloaded and the /data volume is mounted.",
        )

    out_path = Path(tempfile.gettempdir()) / f"voxtral_{uuid.uuid4().hex}.wav"

    cmd = [
        VOXTRAL_BIN, "speak",
        "--text", req.text,
        "--gguf", GGUF_PATH,
        "--tokenizer", TOKENIZER_PATH,
        "--voice", req.voice,
        "--euler-steps", str(req.euler_steps),
        "--max-frames", str(req.max_frames),
        "--output", str(out_path),
    ]

    logger.info(
        "Synthesizing %d chars | voice=%s euler_steps=%d max_frames=%d → %s",
        len(req.text), req.voice, req.euler_steps, req.max_frames, out_path.name,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=SYNTHESIS_TIMEOUT,
            # voxtral resolves voice embeddings relative to CWD as
            # "models/voxtral-tts/voice_embedding/<voice>.safetensors".
            # GGUF_PATH = .../data/models/voxtral/... → cwd = .../data/
            cwd=str(Path(GGUF_PATH).parent.parent.parent),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"voxtral synthesis timed out after {SYNTHESIS_TIMEOUT}s",
        )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        logger.error("voxtral exited %d:\n%s", result.returncode, stderr)
        raise HTTPException(
            status_code=500,
            detail=f"voxtral exited {result.returncode}: {stderr[-600:]}",
        )

    # Primary path: --output file was written
    if out_path.exists() and out_path.stat().st_size > 0:
        wav_bytes = out_path.read_bytes()
        out_path.unlink(missing_ok=True)
        logger.info("Synthesis complete: %d bytes", len(wav_bytes))
        return Response(content=wav_bytes, media_type="audio/wav")

    # Fallback: binary may write WAV to stdout instead of --output file
    if result.stdout:
        logger.info("WAV received via stdout: %d bytes", len(result.stdout))
        out_path.unlink(missing_ok=True)
        return Response(content=result.stdout, media_type="audio/wav")

    raise HTTPException(
        status_code=500,
        detail="voxtral produced no output. Check logs for the binary's stderr.",
    )
