#!/usr/bin/env bash
# start-all.sh — Boot the full Infinity Mystery Video Factory stack:
#   1. Docker Compose (API + Streamlit + WhisperX)
#   2. Google Drive export watcher
#   (Voxtral: start manually from Windows PowerShell via start_voxtral_host.sh)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 1. Docker Compose ─────────────────────────────────────────────────────────
echo "━━━ [1/2] Docker Compose starten..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d

echo "━━━ Warte auf API (http://localhost:8000/health)..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        echo "    ✓ API bereit (nach ${i}x2s)"
        break
    fi
    sleep 2
done

# ── 2. Google Drive Export Watcher ────────────────────────────────────────────
echo "━━━ [2/2] Google Drive Export Watcher starten..."
"$SCRIPT_DIR/export_watcher.sh" &

echo ""
echo "✅ Docker + Watcher laufen."
echo "   UI:      http://localhost:8501"
echo "   API:     http://localhost:8000/docs"
echo ""
echo "⚠️  Voxtral manuell starten (Windows PowerShell):"
echo "   .\\voxtral_windows_bin\\Start-VoxtralService.ps1"
