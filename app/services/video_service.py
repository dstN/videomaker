"""
Video Service — MoviePy-based pipeline that assembles a single video part
from TTS audio, WhisperX word timestamps, and visual assets.

Architecture (5 bausteins):
  1. generate_word_timestamps()  — WhisperX in-process (CUDA, int8_float16)
  2. _load_background()          — loops short BG clip to match TTS duration
  3. _build_audio_mix()          — TTS 100% + ambient 8% with 2s fade-out
  4. _build_subtitle_clips()     — per-word TextClips, CourierPrime-Regular.ttf
  5. render_video()              — CompositeVideoClip → h264_nvenc export

Hardware allocation:
  - WhisperX:  CUDA (compute_type=int8_float16, fits RTX 4060 8GB)
  - Rendering: CUDA via h264_nvenc (MoviePy → ffmpeg)
  - Caller is responsible for running this from a thread pool
    (both whisperx.transcribe and write_videofile are blocking).
"""
from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# ── Layout constants ───────────────────────────────────────────────────────────
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS = 30

# ── Subtitle style ─────────────────────────────────────────────────────────────
SUBTITLE_FONT = "/assets/fonts/SpecialElite-Regular.ttf"  # grunge horror font
SUBTITLE_FONT_SIZE = 72
SUBTITLE_COLOR = "white"
SUBTITLE_STROKE_COLOR = "black"
SUBTITLE_STROKE_WIDTH = 5    # thick stroke makes frayed edges pop on dark BG
# Vertical position: 55% from top → middle third of a vertical frame
SUBTITLE_Y_FRACTION = 0.55
# Words per phrase group for the additive typewriter reveal
PHRASE_SIZE = 5

# ── Audio ──────────────────────────────────────────────────────────────────────
AMBIENT_VOLUME = 0.08      # 8% — voice is always dominant
AMBIENT_FADE_OUT = 2.0     # seconds

# ── WhisperX ──────────────────────────────────────────────────────────────────
WHISPERX_MODEL_SIZE = "base"


# ─────────────────────────────────────────────────────────────────────────────
# Baustein 1 — WhisperX word timestamps (CUDA, in-process)
# ─────────────────────────────────────────────────────────────────────────────

def generate_word_timestamps(
    audio_path: Path,
    language: str = "en",
) -> list[dict[str, Any]]:
    """
    Transcribe audio and return word-level timestamps using WhisperX.

    Hardware: device="cuda", compute_type="int8_float16"
    (INT8 weights + FP16 activations — comfortably fits RTX 4060 8 GB VRAM)

    Returns:
        list of {"word": str, "start": float, "end": float}
    """
    import whisperx  # deferred — only available in GPU environment

    logger.info(
        "WhisperX: loading model '%s' (device=cuda, compute_type=int8_float16)...",
        WHISPERX_MODEL_SIZE,
    )
    model = whisperx.load_model(
        WHISPERX_MODEL_SIZE,
        device="cuda",
        compute_type="int8_float16",
    )

    logger.info("WhisperX: transcribing %s...", audio_path.name)
    result = model.transcribe(str(audio_path), language=language if language != "auto" else None)

    detected_lang = result.get("language", language)
    logger.info("WhisperX: aligning timestamps (language=%s)...", detected_lang)

    align_model, align_metadata = whisperx.load_align_model(
        language_code=detected_lang,
        device="cuda",
    )
    aligned = whisperx.align(
        result["segments"],
        align_model,
        align_metadata,
        str(audio_path),
        device="cuda",
    )

    words: list[dict[str, Any]] = []
    for segment in aligned.get("segments", []):
        for w in segment.get("words", []):
            word_text = w.get("word", "").strip()
            start = w.get("start")
            end = w.get("end")
            if word_text and start is not None and end is not None:
                words.append({"word": word_text, "start": float(start), "end": float(end)})

    logger.info("WhisperX: extracted %d word timestamps.", len(words))
    return words


# ─────────────────────────────────────────────────────────────────────────────
# Baustein 2 — Asset-Manager (background video loop)
# ─────────────────────────────────────────────────────────────────────────────

def _load_background(target_duration: float) -> Any:
    """
    Load assets/backgrounds/stock.mp4, resize + crop to 1080x1920,
    then loop it (via concatenation) until it covers target_duration.

    Falls back to a black frame clip if the file is missing.
    """
    from moviepy import ColorClip, VideoFileClip, concatenate_videoclips

    bg_path = Path("/assets/backgrounds/stock.mp4")

    if not bg_path.exists():
        logger.warning(
            "Background video not found at %s — using solid black fallback.", bg_path
        )
        return ColorClip(
            size=(OUTPUT_WIDTH, OUTPUT_HEIGHT), color=(0, 0, 0)
        ).with_duration(target_duration)

    logger.info("Loading background video (target=%.2fs)...", target_duration)
    raw = VideoFileClip(str(bg_path)).without_audio()

    # Resize so the shorter edge fills the frame, then center-crop
    scale = max(OUTPUT_WIDTH / raw.w, OUTPUT_HEIGHT / raw.h)
    scaled = raw.resized(width=int(raw.w * scale), height=int(raw.h * scale))
    cropped = scaled.cropped(
        x_center=scaled.w / 2,
        y_center=scaled.h / 2,
        width=OUTPUT_WIDTH,
        height=OUTPUT_HEIGHT,
    )

    # Loop via concatenation — guaranteed to work in all MoviePy 2.x versions
    n_loops = math.ceil(target_duration / cropped.duration)
    looped = concatenate_videoclips([cropped] * n_loops).subclipped(0, target_duration)
    logger.info(
        "Background: %.1fs source × %d loops → %.2fs.",
        cropped.duration, n_loops, target_duration,
    )
    return looped


# ─────────────────────────────────────────────────────────────────────────────
# Baustein 3 — Audio mixing (TTS 100% + ambient 8% + 2s fade-out)
# ─────────────────────────────────────────────────────────────────────────────

def _build_audio_mix(tts_path: Path, target_duration: float) -> Any:
    """
    Compose TTS voice (100%) with optional ambient music (8%).

    Ambient is looped to cover target_duration, volume-reduced to AMBIENT_VOLUME,
    and faded out 2 seconds before the end.  If ambient.mp3 is absent, returns
    the TTS clip alone.
    """
    from moviepy import AudioFileClip, CompositeAudioClip, concatenate_audioclips
    from moviepy.audio.fx import AudioFadeOut, MultiplyVolume

    tts_clip = AudioFileClip(str(tts_path)).with_duration(target_duration)

    music_path = Path("/assets/music/ambient.mp3")
    if not music_path.exists():
        logger.warning("Ambient track not found at %s — TTS only.", music_path)
        return tts_clip

    ambient_raw = AudioFileClip(str(music_path))

    # Loop ambient to cover full duration via concatenation
    n_loops = math.ceil(target_duration / ambient_raw.duration)
    ambient_looped = (
        concatenate_audioclips([ambient_raw] * n_loops)
        .subclipped(0, target_duration)
    )

    ambient_mixed = ambient_looped.with_effects([
        MultiplyVolume(AMBIENT_VOLUME),
        AudioFadeOut(AMBIENT_FADE_OUT),
    ])

    logger.info(
        "Audio mix: TTS 100%% + ambient %.0f%% (%.1fs fade-out).",
        AMBIENT_VOLUME * 100, AMBIENT_FADE_OUT,
    )
    return CompositeAudioClip([tts_clip, ambient_mixed])


# ─────────────────────────────────────────────────────────────────────────────
# Baustein 4 — Subtitle engine (typewriter / additive phrase reveal)
# ─────────────────────────────────────────────────────────────────────────────

def _group_into_phrases(
    words: list[dict[str, Any]], phrase_size: int = PHRASE_SIZE
) -> list[list[dict[str, Any]]]:
    """
    Partition the word list into sequential phrases of `phrase_size` words.
    The last phrase may have fewer words.
    """
    return [words[i : i + phrase_size] for i in range(0, len(words), phrase_size)]


def _build_subtitle_clips(words: list[dict[str, Any]]) -> list[Any]:
    """
    Typewriter / additive reveal effect.

    Words are grouped into phrases of PHRASE_SIZE (default 5).  Within each
    phrase the text builds cumulatively, word by word, timed to the WhisperX
    timestamps:

        t=w1.start  → "The"
        t=w2.start  → "The door"
        t=w3.start  → "The door was"
        …
        t=wN.end    → (silence — next phrase begins)

    Each cumulative step gets its own TextClip that replaces the previous one
    (no crossfade — hard cut matches the spoken word boundary).

    Font:   SpecialElite-Regular.ttf (grunge/typewriter horror aesthetic)
    Style:  white fill, thick black stroke (stroke_width=5) for legibility
            on any dark background video.
    """
    from moviepy import TextClip

    subtitle_y = int(OUTPUT_HEIGHT * SUBTITLE_Y_FRACTION)
    # Maximum text width with horizontal padding so nothing clips the edges
    max_text_width = OUTPUT_WIDTH - 80

    clips: list[Any] = []
    phrases = _group_into_phrases(words)

    for phrase in phrases:
        # Build one TextClip per cumulative step inside the phrase
        for i, word_data in enumerate(phrase):
            cumulative_text = " ".join(w["word"] for w in phrase[: i + 1])
            start = word_data["start"]

            # Clip ends when the next word in the phrase starts, or when this
            # word finishes (last word in phrase → phrase disappears cleanly)
            if i < len(phrase) - 1:
                end = phrase[i + 1]["start"]
            else:
                end = word_data["end"]

            duration = max(end - start, 0.05)  # guard against zero-width gap

            clip = (
                TextClip(
                    text=cumulative_text,
                    font=SUBTITLE_FONT,
                    font_size=SUBTITLE_FONT_SIZE,
                    color=SUBTITLE_COLOR,
                    stroke_color=SUBTITLE_STROKE_COLOR,
                    stroke_width=SUBTITLE_STROKE_WIDTH,
                    method="label",
                    size=(max_text_width, None),  # auto-height, bounded width
                )
                .with_position(("center", subtitle_y))
                .with_start(start)
                .with_duration(duration)
            )
            clips.append(clip)

    logger.info(
        "Subtitle engine: %d words → %d phrases → %d additive clips.",
        len(words), len(phrases), len(clips),
    )
    return clips


# ─────────────────────────────────────────────────────────────────────────────
# Baustein 5 — Composition + GPU-accelerated export
# ─────────────────────────────────────────────────────────────────────────────

def render_video(
    tts_path: Path,
    words: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """
    Compose background + subtitles + mixed audio and export as 1080x1920 MP4.

    Codec: h264_nvenc — forces NVIDIA GPU encode.
    All layers are assembled in MoviePy; ffmpeg handles the final mux + encode.

    Args:
        tts_path:    Path to the Voxtral-generated WAV file.
        words:       Word-level timestamps from generate_word_timestamps().
        output_path: Destination .mp4 path (parent dirs created if needed).

    Returns:
        output_path after successful write.
    """
    from moviepy import AudioFileClip, CompositeVideoClip

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine total duration from TTS audio
    with AudioFileClip(str(tts_path)) as probe:
        total_duration = probe.duration
    logger.info(
        "Rendering: duration=%.2fs  output=%s", total_duration, output_path.name
    )

    # Assemble layers
    bg_clip = _load_background(total_duration)
    audio_mix = _build_audio_mix(tts_path, total_duration)
    subtitle_clips = _build_subtitle_clips(words)

    composite = (
        CompositeVideoClip(
            [bg_clip] + subtitle_clips,
            size=(OUTPUT_WIDTH, OUTPUT_HEIGHT),
        )
        .with_duration(total_duration)
        .with_audio(audio_mix)
    )

    # GPU-accelerated export (h264_nvenc → NVIDIA encoder)
    composite.write_videofile(
        str(output_path),
        fps=OUTPUT_FPS,
        codec="h264_nvenc",
        audio_codec="aac",
        audio_bitrate="192k",
        preset="fast",            # nvenc supports: slow/medium/fast/hp/hq/ll…
        ffmpeg_params=["-b:v", "6M", "-pix_fmt", "yuv420p"],
        logger=None,              # suppress MoviePy's built-in progress bar
    )
    composite.close()

    logger.info("Render complete → %s", output_path)
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Public async entry-point
# ─────────────────────────────────────────────────────────────────────────────

async def process_part(
    tts_path: Path,
    output_path: Path,
    language: str = "en",
) -> Path:
    """
    Full async pipeline for one video part:
      1. WhisperX timestamps (blocking CUDA — runs in thread pool)
      2. Compose + render    (blocking GPU  — runs in thread pool)

    Both operations are CPU/GPU-bound and are offloaded from the FastAPI
    event loop via run_in_executor.
    """
    loop = asyncio.get_event_loop()

    logger.info("video_service.process_part: %s → %s", tts_path.name, output_path.name)
    settings = get_settings()

    # Step 1: word timestamps
    words: list[dict[str, Any]] = await loop.run_in_executor(
        None,
        generate_word_timestamps,
        tts_path,
        language,
    )

    # Step 2: render
    result: Path = await loop.run_in_executor(
        None,
        render_video,
        tts_path,
        words,
        output_path,
    )

    return result
