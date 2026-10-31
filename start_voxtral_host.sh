#!/usr/bin/env bash
# start_voxtral_host.sh
# Lädt das vorgefertigte Windows-Binary von voxtral-mini-realtime-rs herunter
# und startet den FastAPI-Wrapper-Service auf dem Windows-Host (Port 8700).
#
# Warum Windows statt WSL2:
#   Vulkan funktioniert in WSL2 NICHT (nur llvmpipe/CPU-Softwarerenderer).
#   Der NVIDIA Windows-Treiber stellt echtes Vulkan zur Verfügung.
#   Docker-Container erreichen den Windows-Host über host.docker.internal:8700.
#
# Voraussetzungen (einmalig):
#   1. Python in Windows: winget install Python.Python.3.12
#   2. pip install fastapi "uvicorn[standard]" (in Windows PowerShell)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_URL="https://github.com/TrevorS/voxtral-mini-realtime-rs/releases/download/v0.2.5/voxtral-mini-realtime-x86_64-pc-windows-msvc.zip"
BIN_DIR="$SCRIPT_DIR/voxtral_windows_bin"
DATA_DIR_WIN="$(wslpath -w "$SCRIPT_DIR/data/models/voxtral")"
SCRIPT_DIR_WIN="$(wslpath -w "$SCRIPT_DIR")"
DATA_WIN="$(wslpath -w "$SCRIPT_DIR/data")"
PORT=8700

# ── 1. Windows-Binary herunterladen (einmalig) ────────────────────────────────
mkdir -p "$BIN_DIR"
BINARY_WIN="$(wslpath -w "$BIN_DIR/voxtral.exe")"

if [ ! -f "$BIN_DIR/voxtral.exe" ]; then
  echo "→ Lade voxtral Windows-Binary (v0.2.5) herunter..."
  curl -L "$RELEASE_URL" -o "$BIN_DIR/voxtral.zip"
  cd "$BIN_DIR"
  unzip -o voxtral.zip
  # Binary ist in einem Unterordner im ZIP
  find . -name "voxtral.exe" -not -path "./voxtral.exe" -exec mv {} . \; 2>/dev/null || true
  rm -f voxtral.zip
  cd "$SCRIPT_DIR"
  echo "→ Binary bereit: $BIN_DIR/voxtral.exe"
else
  echo "→ Binary bereits vorhanden: $BIN_DIR/voxtral.exe"
fi

# ── 2. PowerShell-Startscript generieren ─────────────────────────────────────
PS_SCRIPT="$BIN_DIR/Start-VoxtralService.ps1"
cat > "$PS_SCRIPT" << POWERSHELL
# Start-VoxtralService.ps1
# Startet den Voxtral FastAPI-Wrapper auf Windows (Port $PORT).
# Vulkan läuft auf Windows via NVIDIA-Treiber — kein WSL2-Problem.
# Docker-Container in WSL2 verbinden sich über host.docker.internal:$PORT.

\$env:GGUF_PATH        = "$DATA_DIR_WIN\voxtral-tts-q4.gguf"
\$env:TOKENIZER_PATH   = "$DATA_DIR_WIN\tekken.json"
\$env:VOXTRAL_BIN      = "$BINARY_WIN"
\$env:DEFAULT_EULER_STEPS = "3"
\$env:MESA_D3D12_DEFAULT_ADAPTER_NAME = "NVIDIA"

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  Voxtral TTS Service — Port $PORT" -ForegroundColor Cyan
Write-Host "  Docker erreicht diesen Service via:" -ForegroundColor Cyan
Write-Host "  http://host.docker.internal:$PORT" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

Set-Location "$DATA_WIN"
python -m uvicorn voxtral_service.main:app --host 0.0.0.0 --port $PORT
POWERSHELL

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Voxtral Windows Binary bereit."
echo ""
echo "  Einmalig in Windows PowerShell:"
echo "    pip install fastapi \"uvicorn[standard]\""
echo ""
echo "  Service starten (Windows PowerShell):"
echo "    $SCRIPT_DIR_WIN\\voxtral_windows_bin\\Start-VoxtralService.ps1"
echo ""
echo "  Danach Docker starten:"
echo "    docker compose up api ui whisperx"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
