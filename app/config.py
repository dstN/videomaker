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
    # Voxtral läuft auf dem WSL2-Host (GPU via Vulkan, funktioniert nicht in Docker).
    # start_voxtral_host.sh startet den Service auf Port 8700.
    voxtral_url: str = "http://172.29.176.1:8700"

    # Paths
    data_dir: Path = Path("/data")
    assets_dir: Path = Path("/assets")
    output_dir: Path = Path("/output")
    database_url: str = "sqlite:////data/videomaker.db"

    # Voxtral GGUF model (downloaded to data volume, not assets)
    voxtral_repo_id: str = "TrevorJS/voxtral-tts-q4-gguf"
    voxtral_cache_dir: Path = Path("/data/models/voxtral")

    # Video part length (words). 160-210 words ≈ 60-90 s of narration.
    part_min_words: int = 160
    part_max_words: int = 210

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
