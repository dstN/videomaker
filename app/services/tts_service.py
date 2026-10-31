"""
Voxtral TTS Service -- HTTP client for the voxtral-engine microservice.

The voxtral-mini-realtime-rs Rust binary exposes a REST API on Windows (port 8700).
This module sends synthesis requests and writes the returned WAV bytes to disk.

VRAM constraint (RTX 4060, 8 GB):
  Model weights occupy ~5.7 GB. Intermediate tensors during inference can push
  cubecl's memory pool into an 8 GB single-block allocation -> OOM crash.
  Fix: limit MAX_FRAMES_PER_CALL=300 per call (~24 s of audio at 80 ms/frame).
  For longer texts, _split_into_chunks() chunks the input and WAV chunks are
  concatenated with the stdlib wave module -- no extra dependencies.

euler_steps=3: minimum for intelligible flow-matching TTS output.
  1 step = noise/gibberish. 3 steps = real-time quality. VRAM stays safe because
  euler_steps repeats the same-size tensors; it does NOT grow peak tensor size.
"""
from __future__ import annotations

import asyncio
import audioop
import io
import logging
import re
import subprocess
import wave
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# ── Number-to-words normalisation ─────────────────────────────────────────────
_ONES = [
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + _int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + (" " + _int_to_words(rest) if rest else "")
    if n < 1_000_000:
        rest = n % 1000
        return _int_to_words(n // 1000) + " thousand" + (" " + _int_to_words(rest) if rest else "")
    rest = n % 1_000_000
    return _int_to_words(n // 1_000_000) + " million" + (" " + _int_to_words(rest) if rest else "")


def _year_to_words(n: int) -> str:
    """Convert a 4-digit year to natural spoken form: 1987 → 'nineteen eighty-seven'."""
    century = n // 100
    remainder = n % 100
    if remainder == 0:
        return _int_to_words(century) + " hundred"
    return _int_to_words(century) + " " + _int_to_words(remainder)


def _time_to_words(h: str, m: str) -> str:
    """Convert '4:15' or '4.15' → 'four fifteen'."""
    hw = _int_to_words(int(h))
    mi = int(m)
    return hw + (" oh " + _int_to_words(mi) if 0 < mi < 10 else (" " + _int_to_words(mi) if mi else " o'clock"))


def _normalize_numbers(text: str) -> str:
    """Replace digit-form numbers with spoken-word equivalents so WhisperX can align them."""
    # Time patterns like 4:15 PM or 4.15 pm (before plain decimal handling)
    text = re.sub(
        r'\b(\d{1,2})[:\.](\d{2})\s*(am|pm|AM|PM)\b',
        lambda m: _time_to_words(m.group(1), m.group(2)) + " " + m.group(3).lower(),
        text,
    )
    # Plain decimals like 3.5 → "three point five"
    text = re.sub(
        r'\b(\d+)\.(\d+)\b',
        lambda m: _int_to_words(int(m.group(1))) + " point " + " ".join(_int_to_words(int(d)) for d in m.group(2)),
        text,
    )
    # Years: 4-digit numbers in plausible year range → "nineteen eighty-seven"
    text = re.sub(
        r'\b(1[1-9]\d{2}|20[0-9]{2})\b',
        lambda m: _year_to_words(int(m.group(1))),
        text,
    )
    # Plain integers
    text = re.sub(
        r'\b(\d+)\b',
        lambda m: _int_to_words(int(m.group(1))),
        text,
    )
    return text

# Output sample rate produced by voxtral (24 kHz PCM)
SAMPLE_RATE = 24_000

# euler_steps=8: quality mode (CLI: 3=real-time, 4=balanced, 8=quality).
# Safe on 8 GB VRAM -- step count does NOT increase peak tensor size.
DEFAULT_EULER_STEPS = 8

# max_frames per synthesis call. 600 frames x 80 ms/frame = 48 s of audio.
# 300 was stable; 600 is the next step up. If cubecl OOMs on your GPU,
# drop back to 300 via VOXTRAL_MAX_FRAMES env var in the PS1.
MAX_FRAMES_PER_CALL = 600

# Target chunk size in characters. At ~10-12 chars/s natural TTS rate, 400 chars
# ≈ 33-40 s — comfortably inside the 48 s frame ceiling.
_CHUNK_TARGET_CHARS = 400

# Hard ceiling: no chunk may exceed this. Split at word boundary if needed.
# 480 chars × 10 chars/s ≈ 48 s matches max_frames=600. 10 % safety margin
# ensures the synthesizer always completes the last word before the frame cap.
_CHUNK_HARD_MAX = 480

# Silence inserted between synthesized chunks (samples at 24 kHz, 16-bit).
# 300 ms = 7 200 samples -- gives a natural sentence-boundary breath.
_INTER_CHUNK_SILENCE_MS = 300

# TTS playback speed. 1.0 = native voxtral speed (slightly fast).
# 0.88 = 12% slower — more deliberate, story-telling pace.
# ffmpeg atempo range: 0.5 – 2.0.
_SPEECH_TEMPO = 0.93

# All available voxtral voice presets
AVAILABLE_VOICES: list[str] = [
    "casual_female",
    "casual_male",
    "neutral_female",
    "neutral_male",
]

# Fallback when no voice is stored on the story
_DEFAULT_VOICE = "casual_female"


# -- Error ---------------------------------------------------------------------

class TTSError(Exception):
    """Raised when Voxtral TTS fails unrecoverably."""


# -- Text chunking -------------------------------------------------------------

def _split_into_chunks(text: str, target_chars: int = _CHUNK_TARGET_CHARS) -> list[str]:
    """
    Split text into chunks each strictly ≤ _CHUNK_HARD_MAX characters so that
    no synthesis call ever hits the max_frames ceiling mid-word.

    Splitting priority (highest to lowest naturalness):
      1. Sentence boundaries  (. ! ?)
      2. Clause boundaries    (, ;)
      3. Word boundaries      (fallback — avoids cutting inside a word)

    Chunks are then greedily grouped up to target_chars for fewer HTTP calls.
    """
    # ── Step 1: sentence boundaries ──────────────────────────────────────────
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())

    # ── Step 2: break sentences that still exceed the hard ceiling ───────────
    atomic: list[str] = []
    for sentence in sentences:
        if len(sentence) <= _CHUNK_HARD_MAX:
            atomic.append(sentence)
            continue
        # Try clause boundaries first
        clauses = re.split(r'(?<=[,;])\s+', sentence)
        for clause in clauses:
            if len(clause) <= _CHUNK_HARD_MAX:
                atomic.append(clause)
                continue
            # Last resort: word-boundary split
            words = clause.split()
            current_words: list[str] = []
            current_len = 0
            for word in words:
                word_len = len(word)
                if current_words and current_len + 1 + word_len > _CHUNK_HARD_MAX:
                    atomic.append(" ".join(current_words))
                    current_words = [word]
                    current_len = word_len
                else:
                    current_words.append(word)
                    current_len += (1 if current_words else 0) + word_len
            if current_words:
                atomic.append(" ".join(current_words))

    # ── Step 3: greedily group atomic pieces up to target_chars ──────────────
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for piece in atomic:
        join_len = current_len + (1 if current else 0) + len(piece)
        if current and join_len > target_chars:
            chunks.append(" ".join(current))
            current = [piece]
            current_len = len(piece)
        else:
            current.append(piece)
            current_len = join_len

    if current:
        chunks.append(" ".join(current))

    return chunks


# -- WAV concatenation with RMS normalisation ---------------------------------

_TARGET_RMS = 8000   # target RMS level (out of 32768 for 16-bit PCM)
_MAX_GAIN   = 1.05   # almost no boosting — avoids amplifying TTS model noise floor
_MIN_GAIN   = 0.6    # never cut more than 40 % (keeps loud chunks audible)


def _rms(frames: bytes, sampwidth: int) -> float:
    """Return RMS amplitude of raw PCM frames."""
    rms = audioop.rms(frames, sampwidth)
    return float(rms) if rms > 0 else 1.0


def _normalize_frames(frames: bytes, sampwidth: int, target_rms: float = _TARGET_RMS) -> bytes:
    """Scale PCM frames toward target_rms, clamped to [_MIN_GAIN, _MAX_GAIN]."""
    current = _rms(frames, sampwidth)
    factor = max(_MIN_GAIN, min(_MAX_GAIN, target_rms / current))
    return audioop.mul(frames, sampwidth, factor)


def _silence_frames(framerate: int, sampwidth: int, channels: int, ms: int) -> bytes:
    """Generate ms milliseconds of digital silence."""
    n_samples = int(framerate * ms / 1000) * channels
    return b"\x00" * (n_samples * sampwidth)


def _apply_loudnorm(wav_bytes: bytes) -> bytes:
    """
    Apply EBU R128 loudness normalisation via ffmpeg loudnorm filter.
    Target: -16 LUFS integrated, -1.5 dBTP true peak, LRA 11.
    Returns original bytes if ffmpeg fails.
    """
    import tempfile, uuid
    tmp_in  = Path(tempfile.gettempdir()) / f"ln_in_{uuid.uuid4().hex}.wav"
    tmp_out = Path(tempfile.gettempdir()) / f"ln_out_{uuid.uuid4().hex}.wav"
    try:
        tmp_in.write_bytes(wav_bytes)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(tmp_in),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", str(SAMPLE_RATE),
                str(tmp_out),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0 and tmp_out.exists():
            return tmp_out.read_bytes()
        logger.warning("loudnorm ffmpeg failed (rc=%d); keeping original", result.returncode)
        return wav_bytes
    finally:
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def _apply_tempo(wav_bytes: bytes, tempo: float) -> bytes:
    """
    Stretch/compress WAV speed via ffmpeg atempo without pitch change.
    Returns the tempo-adjusted WAV bytes.
    Silently returns original if ffmpeg is unavailable or tempo==1.0.
    """
    if abs(tempo - 1.0) < 0.01:
        return wav_bytes
    import tempfile, uuid
    tmp_in  = Path(tempfile.gettempdir()) / f"tts_in_{uuid.uuid4().hex}.wav"
    tmp_out = Path(tempfile.gettempdir()) / f"tts_out_{uuid.uuid4().hex}.wav"
    try:
        tmp_in.write_bytes(wav_bytes)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(tmp_in),
                "-af", f"atempo={tempo}",
                "-ar", str(SAMPLE_RATE),
                str(tmp_out),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0 and tmp_out.exists():
            return tmp_out.read_bytes()
        logger.warning("atempo ffmpeg failed (rc=%d); keeping original speed", result.returncode)
        return wav_bytes
    finally:
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def _concat_wavs(wav_blobs: list[bytes]) -> bytes:
    """
    Concatenate WAV blobs with:
    - Soft RMS normalisation per chunk (gain clamped to avoid noise amplification)
    - 300 ms silence padding between chunks for a natural sentence-boundary breath
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out_wav:
        params_set = False
        framerate = SAMPLE_RATE
        sampwidth = 2
        channels = 1
        for i, blob in enumerate(wav_blobs):
            with wave.open(io.BytesIO(blob), "rb") as src:
                if not params_set:
                    out_wav.setparams(src.getparams())
                    framerate = src.getframerate()
                    sampwidth = src.getsampwidth()
                    channels = src.getnchannels()
                    params_set = True
                frames = src.readframes(src.getnframes())
                out_wav.writeframes(frames)
            # insert silence between chunks (not after the last one)
            if i < len(wav_blobs) - 1:
                out_wav.writeframes(
                    _silence_frames(framerate, sampwidth, channels, _INTER_CHUNK_SILENCE_MS)
                )
    return buf.getvalue()


# -- Single-chunk HTTP call ----------------------------------------------------

def _synthesize_chunk(
    client: httpx.Client,
    url: str,
    text: str,
    voice: str,
    euler_steps: int,
) -> bytes:
    """POST one chunk to the voxtral service, return raw WAV bytes."""
    response = client.post(
        url,
        json={
            "text": text,
            "voice": voice,
            "euler_steps": euler_steps,
            "max_frames": MAX_FRAMES_PER_CALL,
        },
    )
    if response.status_code != 200:
        raise TTSError(
            f"voxtral-engine returned HTTP {response.status_code}: {response.text[:400]}"
        )
    wav_bytes = response.content
    if len(wav_bytes) < 44:
        raise TTSError(
            f"voxtral-engine returned {len(wav_bytes)} bytes -- not a valid WAV"
        )
    return wav_bytes


# -- Internal blocking call ----------------------------------------------------

def _generate_blocking(
    text: str,
    output_path: Path,
    voice: str,
    euler_steps: int,
) -> None:
    """
    Split text into <=24-second chunks, synthesize each via the voxtral service,
    concatenate the WAV chunks, and write the result to output_path.
    Must be called from a thread pool -- httpx is used in sync mode here.
    """
    settings = get_settings()
    url = f"{settings.voxtral_url}/synthesize"

    chunks = _split_into_chunks(text)
    logger.info(
        "TTS | voice=%s euler_steps=%d | %d chars -> %d chunk(s) | %.50s...",
        voice, euler_steps, len(text), len(chunks), text,
    )

    wav_blobs: list[bytes] = []
    try:
        with httpx.Client(timeout=600.0) as client:
            for idx, chunk in enumerate(chunks, 1):
                logger.info("  chunk %d/%d: %d chars", idx, len(chunks), len(chunk))
                wav_blobs.append(
                    _synthesize_chunk(client, url, chunk, voice, euler_steps)
                )
    except httpx.ConnectError as exc:
        raise TTSError(
            f"Cannot connect to voxtral-engine at {settings.voxtral_url}. "
            "Is the service running?"
        ) from exc
    except httpx.TimeoutException as exc:
        raise TTSError(
            f"voxtral-engine timed out after 360s for chunk of {len(text)} chars"
        ) from exc

    final_wav = _concat_wavs(wav_blobs) if len(wav_blobs) > 1 else wav_blobs[0]
    final_wav = _apply_tempo(final_wav, _SPEECH_TEMPO)
    final_wav = _apply_loudnorm(final_wav)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(final_wav)
    logger.info(
        "TTS complete: %s (%.1f KB, %d chunk(s))",
        output_path.name, len(final_wav) / 1024, len(wav_blobs),
    )


# -- Public async entrypoint ---------------------------------------------------

async def generate_audio(
    text: str,
    output_path: Path,
    lang_code: str = "en",
    voice: str | None = None,
) -> Path:
    """
    Generate speech audio via the voxtral-engine microservice.

    Args:
        text:        Text to synthesize.
        output_path: Destination .wav path (parent dirs created automatically).
        lang_code:   ISO 639-1 language code (informational only).
        voice:       Voice preset to use. Defaults to _DEFAULT_VOICE.

    Returns:
        Path to the generated .wav file.

    Raises:
        TTSError: If synthesis or HTTP communication fails.
    """
    voice = voice if voice in AVAILABLE_VOICES else _DEFAULT_VOICE
    text = _normalize_numbers(text)
    logger.info("Generating audio | lang=%s -> voice=%s | %d chars", lang_code, voice, len(text))

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            _generate_blocking,
            text,
            output_path,
            voice,
            DEFAULT_EULER_STEPS,
        )
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"Unexpected error during TTS generation: {exc}") from exc

    return output_path
