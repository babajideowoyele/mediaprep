# MediaPrep launcher — starts the local server and opens the browser.
# Usage: right-click > Run with PowerShell, or:  .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
Write-Host "Starting MediaPrep on http://127.0.0.1:7655 ..." -ForegroundColor Cyan
python app.py
