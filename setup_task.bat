@echo off
chcp 65001 >nul
echo ========================================
echo   GitHub Trending Sticky Note - Setup
echo ========================================
echo.

REM ── Create scheduled task (daily at 9 AM) ──
schtasks /create /tn "GitHub Trending Sticky" /tr "python \"C:\Users\Administrator\Desktop\github-sticky\fetch_trending.py\"" /sc daily /st 09:00 /f

if %errorlevel% equ 0 (
    echo [OK] Daily update task created! (Runs every day at 9:00 AM)
) else (
    echo [WARN] Could not create scheduled task. You may need to run as Administrator.
    echo [INFO] Manual update: double-click fetch_trending.py or run this batch file.
)

echo.
echo ── Creating Desktop shortcut ──
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%USERPROFILE%\Desktop\GitHub Trending.lnk'); $s.TargetPath = '%USERPROFILE%\Desktop\github-sticky\sticky.html'; $s.IconLocation = 'shell32.dll,138'; $s.WorkingDirectory = '%USERPROFILE%\Desktop\github-sticky'; $s.Save()"

echo [OK] Desktop shortcut created!
echo.
echo ── Running first update now... ──
python "C:\Users\Administrator\Desktop\github-sticky\fetch_trending.py"

echo.
echo ========================================
echo   All done! The sticky note is ready.
echo   Double-click "GitHub Trending" on desktop to view.
echo ========================================
pause
