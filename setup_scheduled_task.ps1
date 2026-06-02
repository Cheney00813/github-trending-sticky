# Setup daily scheduled task for GitHub Trending Sticky Note update
# Run this ONCE to configure auto-update

$stickyDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $stickyDir "fetch_trending.py"

$taskName = "GitHub Trending Sticky Note Update"
$action = New-ScheduledTaskAction -Execute "python" -Argument "`"$pythonScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At "09:00" -RandomDelay "00:30:00"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "每日更新桌面 GitHub 趋势便签" `
    -Force

Write-Host "[OK] Scheduled task '$taskName' created!"
Write-Host "[INFO] Runs daily at 9:00 AM with 30-min random delay."
Write-Host ""
Write-Host "📋 To run now for the first time, execute:"
Write-Host "   python `"$pythonScript`""
