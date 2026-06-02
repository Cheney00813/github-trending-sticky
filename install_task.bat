@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Creating scheduled task for current user...
schtasks /create /xml "%~dp0schedule_task.xml" /tn "GitHub Trending Sticky" /f 2>&1
if %errorlevel% equ 0 (
    echo [SUCCESS] Scheduled task created!
    echo Updates daily at 9:00 AM + on login.
) else (
    echo [FAIL] Error level: %errorlevel%
    echo.
    echo Please right-click this file ^& select "Run as administrator"
)
