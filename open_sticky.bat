@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   GitHub Trending Sticky Note
echo ============================================

REM ── Check if scheduler is already running ──
tasklist /fi "WindowTitle eq scheduler" /fo csv 2>nul | find /i "python" >nul
if %errorlevel% equ 0 (
    echo [INFO] Scheduler already running.
    goto :open
)

REM ── Start background scheduler ──
echo [INFO] Starting background scheduler...
start "GH-Trending-Scheduler" /min pythonw "%~dp0scheduler.py"
echo [INFO] Scheduler started in background.

REM ── Wait a moment for the first fetch to complete ──
echo [INFO] Waiting for initial data fetch...
timeout /t 5 /nobreak >nul

:open
REM ── Open sticky note ──
echo [INFO] Opening sticky note...
start "" "%~dp0sticky.html"

echo [OK] Done! The sticky note is on your desktop.
echo [TIP] The scheduler updates every 6 hours automatically.
