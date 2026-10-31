"""
Asset auto-setup routine — called once at application startup.
Downloads required files if not already present. Fully idempotent.

Assets managed:
  - CourierPrime-Regular.ttf  →  /assets/fonts/
  - TrevorJS/voxtral-tts-q4-gguf  →  /data/models/voxtral/
"""
from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

COURIER_PRIME_URL = (
    "https://github.com/google/fonts/raw/main/ofl/courierprime/CourierPrime-Regular.ttf"
)
SPECIAL_ELITE_URL = (
    "https://github.com/google/fonts/raw/main/apache/specialelite/SpecialElite-Regular.ttf"
)
VOXTRAL_REPO_ID = "TrevorJS/voxtral-tts-q4-gguf"


# ── Public API ────────────────────────────────────────────────────────────────

def ensure_assets() -> None:
    """
    Download all required assets on first run.
    Safe to call every startup — skips files that already exist.
    """
    from app.config import get_settings  # deferred to avoid circular import at module load
    settings = get_settings()

    _ensure_font(settings.fonts_dir)
    _ensure_voxtral(settings.voxtral_cache_dir)


def get_voxtral_model_path(cache_dir: Path) -> Path:
    """
    Locate the downloaded .gguf file in the Voxtral cache directory.
    Searches recursively so that hf_hub_download subdirectory layouts are handled.
    Validates that the file is a real GGUF binary (not a HuggingFace pointer file).
    Prefers Q4_K_M variant for best quality/speed tradeoff on CPU.
    Raises FileNotFoundError if no valid model has been downloaded yet.
    """
    # Recursive search covers both flat layout (cache_dir/*.gguf) and
    # the HuggingFace hub layout (cache_dir/models--*/snapshots/abc123/*.gguf)
    gguf_files = sorted(cache_dir.rglob("*.gguf"))

    # Filter out pointer/metadata files — real GGUFs start with magic bytes b'GGUF'
    valid = [f for f in gguf_files if _is_valid_gguf(f)]

    if not valid:
        raise FileNotFoundError(
            f"No valid GGUF model found in {cache_dir}. "
            "Ensure the application started correctly (check startup logs for download errors)."
        )

    preferred = [f for f in valid if "q4_k_m" in f.name.lower()]
    return (preferred or valid)[0]


def _is_valid_gguf(path: Path) -> bool:
    """Return True if the file starts with the GGUF magic bytes (not a pointer file)."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_font(fonts_dir: Path) -> None:
    fonts_dir.mkdir(parents=True, exist_ok=True)
    _download_font_if_missing(fonts_dir / "CourierPrime-Regular.ttf", COURIER_PRIME_URL)
    _download_font_if_missing(fonts_dir / "SpecialElite-Regular.ttf", SPECIAL_ELITE_URL)


def _download_font_if_missing(target: Path, url: str) -> None:
    if target.exists():
        logger.debug("Font already present: %s", target)
        return
    logger.info("Downloading %s from Google Fonts...", target.name)
    try:
        urllib.request.urlretrieve(url, target)
        logger.info("Font saved → %s", target)
    except Exception as exc:
        logger.error("Failed to download %s: %s", target.name, exc)
        raise


def _ensure_voxtral(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Migration: move a valid GGUF from a HF hub subdirectory into cache_dir ──
    # hf_hub_download may have stored the file in a nested path such as:
    #   cache_dir/models--TrevorJS--voxtral-tts-q4-gguf/snapshots/<hash>/*.gguf
    # Move it flat into cache_dir so the path is predictable and portable.
    if not list(cache_dir.glob("*.gguf")):
        nested = [f for f in cache_dir.rglob("*.gguf") if _is_valid_gguf(f)]
        if nested:
            dest = cache_dir / nested[0].name
            logger.info("Migrating model from nested path %s → %s", nested[0], dest)
            nested[0].rename(dest)
            logger.info("Migration complete.")

    # Already downloaded (flat layout)?
    flat_ggufs = [f for f in cache_dir.glob("*.gguf") if _is_valid_gguf(f)]
    if flat_ggufs:
        logger.debug("Voxtral GGUF ready at %s", flat_ggufs[0])
        # Still need to ensure tekken.json is present (required by voxtral CLI)
        _ensure_voxtral_tokenizer(cache_dir)
        return

    logger.info(
        "Downloading Voxtral GGUF model from HuggingFace (%s) — no auth token required...",
        VOXTRAL_REPO_ID,
    )
    logger.info("This may take several minutes on first run (model is ~2.5 GB).")

    try:
        from huggingface_hub import list_repo_files, hf_hub_download

        # List available GGUF files in the repo
        all_files = list(list_repo_files(VOXTRAL_REPO_ID, token=None))
        gguf_files = [f for f in all_files if f.endswith(".gguf")]

        if not gguf_files:
            raise RuntimeError(
                f"No .gguf files found in repo {VOXTRAL_REPO_ID}. "
                "Check that the repo ID is correct and accessible."
            )

        # Prefer Q4_K_M for best CPU performance; fall back to first GGUF
        target_file = next(
            (f for f in gguf_files if "q4_k_m" in f.lower()),
            gguf_files[0],
        )
        logger.info("Downloading: %s", target_file)

        downloaded_path = hf_hub_download(
            repo_id=VOXTRAL_REPO_ID,
            filename=target_file,
            local_dir=str(cache_dir),
            token=None,          # Public repo — no auth required
            resume_download=True,  # Resume partial downloads
        )
        logger.info("Voxtral GGUF downloaded → %s", downloaded_path)

    except Exception as exc:
        logger.error("Failed to download Voxtral model: %s", exc)
        raise

    _ensure_voxtral_tokenizer(cache_dir)


def _ensure_voxtral_tokenizer(cache_dir: Path) -> None:
    """Download tekken.json (Voxtral tokenizer) if not already present."""
    tokenizer_path = cache_dir / "tekken.json"
    if tokenizer_path.exists():
        logger.debug("Voxtral tokenizer ready at %s", tokenizer_path)
        return

    logger.info("Downloading Voxtral tokenizer (tekken.json)...")
    try:
        from huggingface_hub import hf_hub_download

        hf_hub_download(
            repo_id=VOXTRAL_REPO_ID,
            filename="tekken.json",
            local_dir=str(cache_dir),
            token=None,
        )
        logger.info("Tokenizer downloaded → %s", tokenizer_path)
    except Exception as exc:
        logger.error("Failed to download tekken.json: %s", exc)
        raise
