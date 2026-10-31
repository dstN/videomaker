from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Google Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"
    gemini_fallback_model: str = "gemma-4-31b-it"  # Used on 429 rate-limit; much higher quota

    # Service URLs
    api_base_url: str = "http://api:8000"
    whisperx_url: str = "http://whisperx:8600"
    # Voxtral läuft auf dem Windows-Host (GPU via Vulkan, funktioniert nicht in Docker).
    # Start-VoxtralService.ps1 startet den Service auf Port 8700. Die Host-IP
    # kann sich nach einem Neustart ändern → VOXTRAL_URL in .env setzen
    # (Windows-IP aus WSL: `ip route show default | awk '{print $3}'`).
    voxtral_url: str = "http://172.29.176.1:8700"

    # Paths
    data_dir: Path = Path("/data")
    assets_dir: Path = Path("/assets")
    output_dir: Path = Path("/output")
    database_url: str = "sqlite:////data/videomaker.db"

    # Voxtral GGUF model (downloaded to data volume, not assets)
    voxtral_repo_id: str = "TrevorJS/voxtral-tts-q4-gguf"
    voxtral_cache_dir: Path = Path("/data/models/voxtral")

    # Voxtral TTS client
    tts_euler_steps: int = 8          # 3=real-time, 4=balanced, 8=quality
    tts_timeout_s: float = 400.0      # > voxtral_service SYNTHESIS_TIMEOUT (360 s)
    tts_retry_attempts: int = 3
    tts_retry_base_delay: float = 10.0

    # Video part length (words). Voxtral speaks ~3.1 words/s (measured on
    # rendered parts), so 370-740 words ≈ 120-240 s of narration.
    part_min_words: int = 370
    part_max_words: int = 740
    # Hard cap per story — longer stories get longer parts, not more parts.
    max_parts: int = 6

    # Rendering
    ffmpeg_preset: str = "fast"
    video_codec: str = "h264_nvenc"
    audio_codec: str = "aac"
    music_volume: float = 0.38

    @property
    def backgrounds_dir(self) -> Path:
        return self.assets_dir / "backgrounds"

    @property
    def music_dir(self) -> Path:
        return self.assets_dir / "music"

    @property
    def fonts_dir(self) -> Path:
        return self.assets_dir / "fonts"

    @property
    def pdf_dir(self) -> Path:
        return self.assets_dir / "pdf"

    @property
    def voice_dir(self) -> Path:
        return self.assets_dir / "voice"

    @property
    def voice_sample_path(self) -> Path:
        return self.voice_dir / "clone_sample.wav"

    @property
    def ai_pdf_path(self) -> Path:
        return self.pdf_dir / "signs_of_ai_writing.pdf"


@lru_cache
def get_settings() -> Settings:
    return Settings()
