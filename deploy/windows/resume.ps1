# Remove the KILL file so the next pass may open positions again.
param(
    [string]$Root = "C:\dexbot"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
$kill = $env:DEXBOT_KILL_FILE
if (Test-Path $kill) {
    Remove-Item -Force $kill
    Write-Host "Removed $kill"
} else {
    Write-Host "No kill switch file at $kill"
}
