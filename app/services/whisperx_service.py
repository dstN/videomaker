from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class WhisperXError(Exception):
    """Raised when the whisperX microservice returns an error."""


async def transcribe(audio_path: Path, language: str = "en") -> list[dict]:
    """
    POST audio path to the whisperX microservice and return word-level timestamps.
    Returns: [{"word": str, "start": float, "end": float}, ...]
    """
    settings = get_settings()
    url = f"{settings.whisperx_url}/transcribe"

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                url,
                json={"audio_path": str(audio_path), "language": language},
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
