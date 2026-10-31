from __future__ import annotations

import json
import logging
import random
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _nvenc_available() -> bool:
    """Return True if h264_nvenc encoder is usable in this environment."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:size=256x256:duration=0.1:rate=1",
                "-c:v", "h264_nvenc", "-frames:v", "1",
                "-f", "null", "/dev/null",
            ],
            capture_output=True,
            timeout=10,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    logger.info("h264_nvenc probe: %s", "available" if ok else "NOT available — falling back to libx264")
    return ok

# Caption font size (px in 1080x1920 vertical frame)
FONT_SIZE = 96  # bigger — single word at a time

# Golden ratio subtitle Y: 0.618 × 1920 ≈ 1187px from top.
# \pos(x,y) with alignment=5 (center) places the text center at that point.
_GOLDEN_Y = 1187

# Fixed X center for the 1080-wide frame
_CENTER_X = 540

# Header cluster: sits at the bottom of the top-1/3 zone (~640px mark)
_TITLE_Y  = 560   # story title  (~29% from top)
_PARTNO_Y = 645   # "Part X / Y" (~34% from top)
_FOLLOW_Y = 725   # "Follow for Part X" CTA (~38% from top)

# On-screen labels per language: (part counter, follow CTA)
_LABELS: dict[str, tuple[str, str]] = {
    "en": ("PART {n} / {total}", "Follow for Part {next}"),
    "de": ("TEIL {n} / {total}", "Folgen für Teil {next}"),
    "fr": ("PARTIE {n} / {total}", "Abonne-toi pour la partie {next}"),
    "es": ("PARTE {n} / {total}", "Sígueme para la parte {next}"),
}

# Tail padding & fade
_TAIL_SILENCE_S = 3.5   # seconds of silence appended after last spoken word
_FADE_OUT_S     = 1.5   # duration of the fade-to-black at video end
_FADE_IN_S      = 0.5   # duration of the fade-from-black at video start


def _ass_escape(text: str) -> str:
    """
    Make arbitrary text safe for an ASS Dialogue line: braces start override
    blocks and backslashes start escapes (\\N, \\h), so they are replaced.
    """
    return (
        text.replace("\\", "/")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _pick_random(directory: Path, extensions: list[str]) -> Path | None:
    files = [f for f in directory.iterdir() if f.suffix.lower() in extensions]
    return random.choice(files) if files else None


def _fill_timestamps(words: list[dict], audio_duration: float) -> list[dict]:
    """
    Interpolate timestamps for words WhisperX failed to align (start==0.0 AND end==0.0).

    This happens with numbers / years: WhisperX normalises "nineteen eighty-seven"
    back to "1987" in its output, then can't align "1987" to the phoneme stream,
    giving start=0.0, end=0.0. Rather than dropping the word entirely (which cuts it
    from subtitles), we spread it proportionally — by character count — across the
    time window between the last good word and the next good word.

    Words that genuinely have valid timestamps are left untouched.
    The very first word of a segment may legitimately start at 0.0 with a real end.
    """
    result = [dict(w) for w in words]  # shallow copy each dict
    n = len(result)
    if n == 0:
        return result

    def _bad(w: dict) -> bool:
        return w.get("start", -1) == 0.0 and w.get("end", -1) == 0.0

    i = 0
    while i < n:
        if not _bad(result[i]):
            i += 1
            continue

        # Found start of an unaligned run — extend to find the full run
        run_start = i
        while i < n and _bad(result[i]):
            i += 1
        run_end = i  # exclusive

        # Left boundary: end of the previous good word (or 0.0 if this is the start)
        t_left = result[run_start - 1].get("end") or 0.0 if run_start > 0 else 0.0

        # Right boundary: start of the next good word (or audio_duration if at the end)
        t_right = result[run_end].get("start") or audio_duration if run_end < n else audio_duration

        # Guard against degenerate windows
        if t_right <= t_left:
            t_right = t_left + 0.15 * (run_end - run_start)

        duration = t_right - t_left
        chars = [max(1, len(result[j].get("word") or "")) for j in range(run_start, run_end)]
        total_chars = sum(chars)

        t = t_left
        for j, char_count in zip(range(run_start, run_end), chars):
            word_dur = duration * char_count / total_chars
            result[j]["start"] = round(t, 3)
            result[j]["end"] = round(t + word_dur, 3)
            t += word_dur

    return result


def _word_events(words: list[dict], audio_duration: float = 0.0) -> list[dict]:
    """
    One ASS event per word, uppercase, seamlessly timed.
    Each event lasts exactly until the next word begins.
    Missing timestamps (start==end==0.0) are filled by interpolation before processing.
    """
    filled = _fill_timestamps(words, audio_duration)
    # Only skip truly empty words (no text at all)
    timed = [w for w in filled if w.get("word", "").strip()]
    events: list[dict] = []
    for i, w in enumerate(timed):
        start = w.get("start", 0.0)
        if i + 1 < len(timed):
            end = timed[i + 1].get("start", w.get("end", start + 0.2))
        else:
            end = w.get("end", start + 0.5)
        events.append({"text": w["word"].strip().upper(), "start": start, "end": end})
    return events


def render_part(
    job_id: int,
    part_number: int,
    total_parts: int,
    story_title: str,
    audio_path: Path,
    timestamps_path: Path,
    output_path: Path,
    language_code: str = "en",
) -> Path:
    """
    Render one video part using ffmpeg directly (avoids moviepy GIL limitations).
    Uses h264_nvenc hardware encoding.
    Returns path to rendered .mp4.
    """
    settings = get_settings()

    # Load word timestamps
    with open(timestamps_path) as f:
        words: list[dict] = json.load(f)

    # Select random background and music
    bg_path = _pick_random(settings.backgrounds_dir, [".mp4", ".mov"])
    music_path = _pick_random(settings.music_dir, [".mp3", ".wav"])

    if bg_path is None:
        raise FileNotFoundError(
            f"No background video files found in {settings.backgrounds_dir}. "
            "Please add .mp4 files to assets/backgrounds/."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Compute total video duration.
    # Use actual WAV duration as ground truth — WhisperX timestamps can be
    # shorter than the audio when the last words have alignment failures (end=0.0).
    with wave.open(str(audio_path), "rb") as wf:
        wav_duration = wf.getnframes() / wf.getframerate()

    # Last word end from whisperx (ignoring zero-aligned words)
    # Last word end from whisperx (ignoring zero-aligned words for end-time calculation)
    timed_words = [w for w in words if not (w.get("start", -1) == 0.0 and w.get("end", -1) == 0.0)]
    whisperx_end = timed_words[-1].get("end", 0.0) if timed_words else 0.0

    # Always use whichever is larger — wav wins if whisperx fell short
    audio_end = max(wav_duration, whisperx_end)
    if wav_duration > whisperx_end + 0.5:
        logger.warning(
            "Part %d: WAV duration (%.2fs) > last whisperx timestamp (%.2fs) — "
            "using WAV duration to avoid premature cutoff.",
            part_number, wav_duration, whisperx_end,
        )
    total_dur = audio_end + _TAIL_SILENCE_S

    # Generate ASS subtitle file from word timestamps
    ass_path = output_path.with_suffix(".ass")
    _write_ass_subtitles(
        words, ass_path, part_number, total_parts, story_title, total_dur, wav_duration, language_code
    )

    # Build ffmpeg command
    cmd = _build_ffmpeg_cmd(
        bg_path=bg_path,
        audio_path=audio_path,
        music_path=music_path,
        ass_path=ass_path,
        output_path=output_path,
        settings=settings,
        total_duration=total_dur,
    )

    logger.info("Starting ffmpeg render for job %d: %s", job_id, output_path.name)
    logger.debug("ffmpeg cmd: %s", " ".join(cmd))

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if proc.returncode != 0:
        err = proc.stderr[-2000:]  # last 2k chars of ffmpeg stderr
        if "out of memory" in err.lower() or "cuda" in err.lower():
            raise RuntimeError(f"CUDA out of memory during render: {err}")
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {err}")

    logger.info("Render complete: %s", output_path)
    return output_path


def _build_ffmpeg_cmd(
    bg_path: Path,
    audio_path: Path,
    music_path: Path | None,
    ass_path: Path,
    output_path: Path,
    settings,
    total_duration: float = 0.0,
) -> list[str]:
    """Construct the ffmpeg command, preferring h264_nvenc with libx264 fallback."""
    if _nvenc_available():
        vcodec = settings.video_codec  # h264_nvenc
        preset = settings.ffmpeg_preset
    else:
        vcodec = "libx264"
        preset = "medium"
    cmd = [
        "ffmpeg", "-y",
        # Loop background video
        "-stream_loop", "-1",
        "-i", str(bg_path),
        # TTS audio (primary)
        "-i", str(audio_path),
    ]

    if music_path:
        cmd += ["-i", str(music_path)]
        logger.info("Music: %s  volume=%.2f", music_path.name, settings.music_volume)
    else:
        logger.warning("No music file found in %s — rendering without music.", settings.music_dir)

    ass_escaped = str(ass_path).replace("\\", "/").replace(":", r"\:")
    fonts_dir = "/assets/fonts"
    fade_start = max(0.0, total_duration - _FADE_OUT_S)
    scale_sub = (
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"subtitles='{ass_escaped}':fontsdir='{fonts_dir}',"
        f"fade=t=in:st=0:d={_FADE_IN_S:.3f},"
        f"fade=t=out:st={fade_start:.3f}:d={_FADE_OUT_S:.3f}"
    )

    if music_path:
        # Merge video and audio filters into a single filter_complex
        cmd += [
            "-filter_complex",
            f"[0:v]{scale_sub}[vout];"
            f"[1:a]apad=pad_dur={_TAIL_SILENCE_S},volume=1.0[tts];"
            f"[2:a]volume={settings.music_volume},aloop=loop=-1:size=2147483647[music];"
            f"[tts][music]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]",
            "-map", "[vout]",
            "-map", "[aout]",
        ]
    else:
        cmd += [
            "-filter_complex",
            f"[0:v]{scale_sub}[vout];"
            f"[1:a]apad=pad_dur={_TAIL_SILENCE_S},volume=1.0[aout]",
            "-map", "[vout]",
            "-map", "[aout]",
        ]

    cmd += [
        "-c:v", vcodec,
        "-preset", preset,
        "-c:a", settings.audio_codec,
        "-b:a", "192k",
        "-t", f"{total_duration:.3f}",
        str(output_path),
    ]
    return cmd


def _secs_to_ass_time(secs: float) -> str:
    """Convert seconds to ASS timestamp h:mm:ss.cc"""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _write_ass_subtitles(
    words: list[dict],
    ass_path: Path,
    part_number: int,
    total_parts: int,
    story_title: str = "",
    total_duration: float = 0.0,
    audio_duration: float = 0.0,
    language_code: str = "en",
) -> None:
    """
    Write an ASS subtitle file with:
    - Persistent header: story title + "Part X / Y" (full video duration)
    - Single-word uppercase captions at the golden-ratio point (\\pos override)
    - "Follow for Part N+1" CTA (last 3 s, non-final parts only)
    """
    font_name = "Courier Prime"

    events    = _word_events(words, audio_duration)
    # WAV duration is ground truth; the last word may be unaligned (end=0.0).
    audio_end = max(audio_duration, words[-1].get("end", 0.0) if words else 0.0)
    part_fmt, follow_fmt = _LABELS.get(language_code, _LABELS["en"])
    vid_end   = total_duration if total_duration > audio_end else audio_end
    t0        = _secs_to_ass_time(0.0)
    tend      = _secs_to_ass_time(vid_end)

    fmt_style = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    # All styles use alignment=5 (middle-center); position set per-event via \\pos().
    style_default = (
        f"Style: Default,{font_name},{FONT_SIZE},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,5,0,5,0,0,0,1"
    )
    style_header = (
        f"Style: Header,{font_name},52,"
        "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,4,0,5,0,0,0,1"
    )
    style_cta = (
        f"Style: CTA,{font_name},46,"
        "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "0,1,0,0,100,100,0,0,1,3,0,5,0,0,0,1"
    )

    lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        fmt_style,
        style_default,
        style_header,
        style_cta,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # ── Persistent header (whole video) ───────────────────────────────────────
    if story_title:
        lines.append(
            f"Dialogue: 0,{t0},{tend},Header,,0,0,0,,"
            f"{{\\pos({_CENTER_X},{_TITLE_Y})}}{_ass_escape(story_title.upper())}"
        )

    part_label = part_fmt.format(n=part_number, total=total_parts)
    lines.append(
        f"Dialogue: 0,{t0},{tend},Header,,0,0,0,,"
        f"{{\\pos({_CENTER_X},{_PARTNO_Y})}}{part_label}"
    )

    # ── Word subtitles at golden-ratio position ───────────────────────────────
    for ev in events:
        start = _secs_to_ass_time(ev["start"])
        end   = _secs_to_ass_time(ev["end"])
        lines.append(
            f"Dialogue: 0,{start},{end},Default,,0,0,0,,"
            f"{{\\pos({_CENTER_X},{_GOLDEN_Y})}}{_ass_escape(ev['text'])}"
        )

    # ── "Follow for Part N+1" CTA (last 3 s, non-final parts only) ───────────
    if part_number < total_parts and audio_end > 3.0:
        follow_label = follow_fmt.format(next=part_number + 1)
        follow_start = _secs_to_ass_time(audio_end - 3.0)
        follow_end   = _secs_to_ass_time(vid_end)
        lines.append(
            f"Dialogue: 0,{follow_start},{follow_end},CTA,,0,0,0,,"
            f"{{\\pos({_CENTER_X},{_FOLLOW_Y})}}{follow_label}"
        )

    ass_path.write_text("\n".join(lines), encoding="utf-8")
    logger.debug("ASS written: %s  (%d word events)", ass_path.name, len(events))
