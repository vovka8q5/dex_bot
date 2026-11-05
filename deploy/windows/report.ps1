# Print win rate, PnL, and the exchange cash figure. Does not place orders.
param(
    [string]$Root = "C:\dexbot"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
& $script:DexbotPython -m dexbot report --config (Join-Path $Root "config\default.yaml")
exit $LASTEXITCODE
