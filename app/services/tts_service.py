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
import io
import logging
import re
import subprocess
import time
import wave
from pathlib import Path

import httpx
from num2words import num2words

from app.config import get_settings

logger = logging.getLogger(__name__)

# ── Number-to-words normalisation ─────────────────────────────────────────────
# Voxtral reads digits unreliably and WhisperX cannot align them, so numbers are
# spelled out in the story's language before synthesis.

_DECIMAL_WORD = {"en": "point", "de": "Komma", "fr": "virgule", "es": "coma"}


def _n2w(n: int, lang: str, **kwargs) -> str:
    try:
        return num2words(n, lang=lang, **kwargs)
    except NotImplementedError:
        return num2words(n, lang="en", **kwargs)


def _time_to_words(h: str, m: str, lang: str) -> str:
    """'4:15' → 'four fifteen' (en) / 'vier Uhr fünfzehn' (de)."""
    hour, minute = int(h), int(m)
    if lang == "de":
        return _n2w(hour, lang) + " Uhr" + (" " + _n2w(minute, lang) if minute else "")
    hw = _n2w(hour, lang)
    if minute == 0:
        return hw + " o'clock"
    if minute < 10:
        return hw + " oh " + _n2w(minute, lang)
    return hw + " " + _n2w(minute, lang)


def _normalize_numbers(text: str, lang: str = "en") -> str:
    """Replace digit-form numbers with spoken words in the given language."""
    if lang == "de":
        # 16:15, 16:15 Uhr, 16.15 Uhr
        text = re.sub(
            r"\b(\d{1,2}):(\d{2})(?:\s*Uhr)?\b|\b(\d{1,2})\.(\d{2})\s*Uhr\b",
            lambda m: _time_to_words(m.group(1) or m.group(3), m.group(2) or m.group(4), lang),
            text,
        )
        decimal_sep = ","
    else:
        # 4:15 PM / 4.15 pm
        text = re.sub(
            r"\b(\d{1,2})[:\.](\d{2})\s*(am|pm|AM|PM)\b",
            lambda m: _time_to_words(m.group(1), m.group(2), lang) + " " + m.group(3).lower(),
            text,
        )
        decimal_sep = "."

    # Decimals: 3.5 (en) / 3,5 (de) → "three point five" / "drei Komma fünf"
    point = _DECIMAL_WORD.get(lang, _DECIMAL_WORD["en"])
    text = re.sub(
        rf"\b(\d+){re.escape(decimal_sep)}(\d+)\b",
        lambda m: _n2w(int(m.group(1)), lang) + f" {point} "
        + " ".join(_n2w(int(d), lang) for d in m.group(2)),
        text,
    )
    # Years: 4-digit numbers in plausible year range
    text = re.sub(
        r"\b(1[1-9]\d{2}|20[0-9]{2})\b",
        lambda m: _n2w(int(m.group(1)), lang, to="year"),
        text,
    )
    # Plain integers
    text = re.sub(r"\b(\d+)\b", lambda m: _n2w(int(m.group(1)), lang), text)
    return text


def spoken_text(text: str, lang: str = "en") -> str:
    """The exact text Voxtral is asked to speak (also used for subtitle alignment)."""
    return _normalize_numbers(text, lang)

# Output sample rate produced by voxtral (24 kHz PCM)
SAMPLE_RATE = 24_000

# euler_steps (CLI: 3=real-time, 4=balanced, 8=quality) comes from
# settings.tts_euler_steps. Safe on 8 GB VRAM -- step count does NOT increase
# peak tensor size, but synthesis time grows linearly with it.

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


# -- WAV helpers ---------------------------------------------------------------

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
    Concatenate WAV blobs with 300 ms silence padding between chunks for a
    natural sentence-boundary breath. Loudness is normalised afterwards by
    _apply_loudnorm().
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


def _is_retryable_tts_error(exc: Exception) -> bool:
    """Timeouts and server-side errors are worth retrying; a dead service is not."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, TTSError) and "HTTP 5" in str(exc)


def _synthesize_with_retry(
    client: httpx.Client,
    url: str,
    chunk: str,
    voice: str,
    euler_steps: int,
    label: str,
) -> bytes:
    settings = get_settings()
    attempts = max(1, settings.tts_retry_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return _synthesize_chunk(client, url, chunk, voice, euler_steps)
        except httpx.ConnectError as exc:
            raise TTSError(
                f"Cannot connect to voxtral-engine at {settings.voxtral_url}. "
                "Is the service running?"
            ) from exc
        except (httpx.TimeoutException, TTSError) as exc:
            if attempt == attempts or not _is_retryable_tts_error(exc):
                if isinstance(exc, httpx.TimeoutException):
                    raise TTSError(
                        f"voxtral-engine timed out after {settings.tts_timeout_s:.0f}s on {label} "
                        f"({len(chunk)} chars, {attempts} attempt(s))"
                    ) from exc
                raise
            delay = settings.tts_retry_base_delay * 2 ** (attempt - 1)
            logger.warning("TTS %s failed (%s) — retry %d/%d in %.0fs",
                           label, str(exc)[:120], attempt, attempts - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


# -- Internal blocking call ----------------------------------------------------

def _generate_blocking(
    text: str,
    output_path: Path,
    voice: str,
    euler_steps: int,
) -> None:
    """
    Split text into short chunks, synthesize each via the voxtral service,
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
    with httpx.Client(timeout=settings.tts_timeout_s) as client:
        for idx, chunk in enumerate(chunks, 1):
            label = f"chunk {idx}/{len(chunks)}"
            logger.info("  %s: %d chars", label, len(chunk))
            wav_blobs.append(
                _synthesize_with_retry(client, url, chunk, voice, euler_steps, label)
            )

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
        lang_code:   ISO 639-1 language code (used to spell out numbers).
        voice:       Voice preset to use. Defaults to _DEFAULT_VOICE.

    Returns:
        Path to the generated .wav file.

    Raises:
        TTSError: If synthesis or HTTP communication fails.
    """
    voice = voice if voice in AVAILABLE_VOICES else _DEFAULT_VOICE
    text = spoken_text(text, lang_code)
    logger.info("Generating audio | lang=%s -> voice=%s | %d chars", lang_code, voice, len(text))

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            _generate_blocking,
            text,
            output_path,
            voice,
            get_settings().tts_euler_steps,
        )
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"Unexpected error during TTS generation: {exc}") from exc

    return output_path
