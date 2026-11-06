# MediaPrep launcher — checks what is needed, then starts the local server and opens the browser.
# Usage: double-click "Start MediaPrep.bat", or run:  .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# 1. Python
$py = $null
foreach ($c in @("python", "py")) { if (Have $c) { $py = $c; break } }
if (-not $py) {
    Write-Host "Python is not installed." -ForegroundColor Yellow
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then start MediaPrep again."
    if (Have "winget") {
        $a = Read-Host "Or install Python 3.12 now with winget? (y/n)"
        if ($a -eq "y") { winget install -e --id Python.Python.3.12; Write-Host "Done. Close this window and start MediaPrep again." }
    }
    Read-Host "Press Enter to close"; exit 1
}

# 2. ffmpeg (needed for Chunkle, Archive Check and Scrub)
if (-not (Have "ffmpeg") -or -not (Have "ffprobe")) {
    Write-Host "ffmpeg is not installed. Chunkle, Archive Check and Scrub need it." -ForegroundColor Yellow
    if (Have "winget") {
        $a = Read-Host "Install ffmpeg now with winget? (y/n)"
        if ($a -eq "y") {
            winget install -e --id Gyan.FFmpeg
            Write-Host "ffmpeg installed. Close this window and start MediaPrep again so Windows picks it up." -ForegroundColor Cyan
            Read-Host "Press Enter to close"; exit 0
        }
    } else {
        Write-Host "Download it from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH."
    }
}

# 3. Optional extras (Transcribe needs faster-whisper; image scrub needs Pillow)
$missing = & $py -c "import importlib.util as u; print(' '.join(p for p in ('faster_whisper','PIL') if u.find_spec(p) is None))"
if ($missing.Trim()) {
    Write-Host "Optional: Transcribe and image scrubbing need extra packages ($missing)." -ForegroundColor DarkGray
    $a = Read-Host "Install them now? Chunkle works without them. (y/n)"
    if ($a -eq "y") { & $py -m pip install --user faster-whisper Pillow }
}

Write-Host "Starting MediaPrep on http://127.0.0.1:7655 ... (close this window to stop it)" -ForegroundColor Cyan
& $py app.py
