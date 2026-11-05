# Create the KILL file. The running service closes positions on its next pass and opens none.
param(
    [string]$Root = "C:\dexbot"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
$kill = $env:DEXBOT_KILL_FILE
New-Item -ItemType Directory -Force -Path (Split-Path $kill) | Out-Null
New-Item -ItemType File -Force -Path $kill | Out-Null
Write-Host "Kill switch file: $kill"
Write-Host "The service closes open positions on the next hourly pass and will not open new ones."
Write-Host "To close immediately: stop Dexbot, then run once with --kill-switch, then start it again."
Write-Host "Resume later with: powershell -File `"$Root\deploy\windows\resume.ps1`""
