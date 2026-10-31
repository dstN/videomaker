#!/usr/bin/env bash
# export_watcher.sh — Run on WSL host (NOT inside Docker).
#
# Usage:
#   ./export_watcher.sh          # foreground (Ctrl+C to stop)
#   ./export_watcher.sh &        # background
#
# Watches output/en/*/  for ".done" sentinel files written by the pipeline.
# When found: copies finished .mp4 files + description.txt to Google Drive
# via PowerShell (no WSL mount required), then deletes the local story folder.

set -uo pipefail

OUTPUT_BASE="$(cd "$(dirname "$0")" && pwd)/output/en"
GDRIVE_WIN='G:\Meine Ablage\TikTok NoSleep Stories'
POLL_INTERVAL=10

# ── Auto-detect WSL UNC base (e.g. \\wsl.localhost\Ubuntu_V2) ────────────────
# wslpath -w / → \\wsl.localhost\Ubuntu_V2\  (trailing backslash = root)
WSL_UNC_BASE=$(wslpath -w / 2>/dev/null | tr -d '\r' | sed 's|\\$||')
if [[ -z "$WSL_UNC_BASE" ]]; then
    echo "[export_watcher] ERROR: wslpath not available. Are you running inside WSL2?" >&2
    exit 1
fi

echo "[export_watcher] Started."
echo "[export_watcher] Watching : $OUTPUT_BASE"
echo "[export_watcher] Target   : $GDRIVE_WIN"
echo "[export_watcher] WSL base : $WSL_UNC_BASE"
echo ""

# ── Sanitize a string for use as a Windows folder name ───────────────────────
sanitize_win() {
    local s="$1"
    # Remove Windows-illegal characters: \ / : * ? " < > |
    echo "$s" | sed 's|[\\/:*?"<>|]||g' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# ── Convert an absolute WSL path to its Windows UNC equivalent ───────────────
to_win_path() {
    # /home/dstn/... → \\wsl.localhost\Ubuntu_V2\home\dstn\...
    echo "${WSL_UNC_BASE}$(echo "$1" | sed 's|/|\\|g')"
}

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do
    # Use nullglob-style check to handle no-match gracefully
    for done_file in "$OUTPUT_BASE"/*/.done; do
        [[ -f "$done_file" ]] || continue

        story_dir=$(dirname "$done_file")
        story_id=$(basename "$story_dir")

        # Read title from sentinel JSON {"title": "..."} — fallback to empty
        raw_title=$(python3 -c "
import json, sys
try:
    d = json.load(open('$done_file'))
    print(d.get('title', '').strip())
except Exception:
    pass
" 2>/dev/null || true)

        safe_title=$(sanitize_win "$raw_title")
        if [[ -n "$safe_title" ]]; then
            folder_name="${story_id} - ${safe_title}"
        else
            folder_name="$story_id"
        fi

        win_src=$(to_win_path "$story_dir")
        win_dst="${GDRIVE_WIN}\\${folder_name}"

        echo "[export_watcher] ► Story $story_id: copying to Google Drive..."
        echo "                   src : $win_src"
        echo "                   dst : $win_dst"

        if powershell.exe -NoProfile -NonInteractive -Command "
\$src = \"$win_src\"
\$dst = \"$win_dst\"
New-Item -ItemType Directory -Force -Path \$dst | Out-Null
\$files = Get-ChildItem -LiteralPath \$src -File |
    Where-Object { \$_.Extension -eq '.mp4' -or \$_.Name -eq 'description.txt' }
\$files | ForEach-Object {
    Copy-Item -LiteralPath \$_.FullName -Destination \$dst -Force -ErrorAction SilentlyContinue
}
\$copied = (Get-ChildItem -LiteralPath \$dst -File -ErrorAction SilentlyContinue |
    Where-Object { \$_.Extension -eq '.mp4' -or \$_.Name -eq 'description.txt' }).Count
Write-Host ('  Copied/verified ' + \$copied + ' file(s) → ' + \$dst)
if (\$copied -lt \$files.Count) { exit 1 }
" 2>&1; then
            echo "[export_watcher] ✓ Export done — deleting local folder: $story_dir"
            rm -rf "$story_dir"
        else
            echo "[export_watcher] ✗ PowerShell failed — local files kept for story $story_id." >&2
        fi

        echo ""
    done

    sleep "$POLL_INTERVAL"
done
