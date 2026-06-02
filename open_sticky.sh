#!/usr/bin/env bash
# ── GitHub Trending Sticky Note Launcher (macOS / Linux) ──
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STICKY_HTML="$SCRIPT_DIR/sticky.html"
SCHEDULER="$SCRIPT_DIR/scheduler.py"
PID_FILE="$SCRIPT_DIR/.scheduler.pid"

echo "============================================"
echo "  GitHub Trending Sticky Note"
echo "============================================"

# ── Check prerequisites ──
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "[FATAL] Python not found. Install Python 3.9+ first."
    exit 2
fi

PYTHON=$(command -v python3 || command -v python)

if ! command -v gh &>/dev/null; then
    echo "[FATAL] GitHub CLI not found. Install from https://cli.github.com"
    exit 2
fi

# ── Kill existing scheduler if running ──
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[INFO] Stopping existing scheduler (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# ── Start background scheduler ──
echo "[INFO] Starting background scheduler..."
$PYTHON "$SCHEDULER" &
SCHED_PID=$!
echo $SCHED_PID > "$PID_FILE"
echo "[INFO] Scheduler started (PID $SCHED_PID)"

# ── Wait for first fetch ──
echo "[INFO] Waiting for initial data fetch..."
sleep 5

# ── Open sticky note ──
echo "[INFO] Opening sticky note..."
if command -v open &>/dev/null; then
    open "$STICKY_HTML"          # macOS
elif command -v xdg-open &>/dev/null; then
    xdg-open "$STICKY_HTML"      # Linux
else
    echo "[WARN] Could not open browser automatically."
    echo "       Open this file manually: $STICKY_HTML"
fi

echo "[OK] Done! The sticky note is on your desktop."
echo "[TIP] Scheduler updates every 6 hours (PID $SCHED_PID)."
