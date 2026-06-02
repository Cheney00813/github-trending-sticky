# Launch GitHub Trending Sticky Note on Desktop
# Uses Edge in app mode for a clean, borderless sticky note feel

$stickyDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$htmlPath = Join-Path $stickyDir "sticky.html"
$fileUrl = "file:///$($htmlPath -replace '\\', '/' -replace ' ', '%20')"

# Kill existing sticky note window if running
Get-Process | Where-Object { $_.MainWindowTitle -like "*GitHub Trending*" } | Stop-Process -Force -ErrorAction SilentlyContinue

# Screen dimensions for positioning (top-right corner)
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$width = 400
$height = 650
$x = $screen.Width - $width - 20
$y = 20

# Try Edge first (app mode = no tabs/address bar), fallback to default browser
$edgePaths = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe"
)

$launched = $false
foreach ($edgePath in $edgePaths) {
    if (Test-Path $edgePath) {
        Write-Host "[INFO] Launching with Edge app mode..."
        Start-Process -FilePath $edgePath -ArgumentList @(
            "--app=$fileUrl",
            "--window-size=$width,$height",
            "--window-position=$x,$y"
        )
        $launched = $true
        break
    }
}

if (-not $launched) {
    Write-Host "[INFO] Edge not found, opening with default browser..."
    # Fallback: open with default browser via a small trick using IE COM or just shell
    Start-Process $htmlPath
}

Write-Host "[OK] Sticky note launched!"
