from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class WhisperXError(Exception):
    """Raised when the whisperX microservice returns an error."""


async def transcribe(audio_path: Path, language: str = "en", text: str | None = None) -> list[dict]:
    """
    POST audio path to the whisperX microservice and return word-level timestamps.
    With text (the exact spoken script) the service force-aligns instead of
    transcribing, so subtitles match the script.
    Returns: [{"word": str, "start": float, "end": float}, ...]
    """
    settings = get_settings()
    url = f"{settings.whisperx_url}/transcribe"

    # Generous timeout: the first request per language also loads the align model.
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(
                url,
                json={"audio_path": str(audio_path), "language": language, "text": text},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WhisperXError(
                f"WhisperX returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise WhisperXError(f"WhisperX request failed: {exc}") from exc

    data = response.json()
    words = data.get("words", [])
    logger.info("WhisperX returned %d word timestamps for %s", len(words), audio_path.name)
    return words
