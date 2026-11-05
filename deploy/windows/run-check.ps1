# Ask Bybit whether the demo key is present, read-only, and allowed to withdraw.
param(
    [string]$Root = "C:\dexbot"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
& $script:DexbotPython -m dexbot check --config (Join-Path $Root "config\default.yaml")
exit $LASTEXITCODE
