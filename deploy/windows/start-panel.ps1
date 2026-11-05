# Local dashboard. Listens on 127.0.0.1 only. Do not publish this port.
param(
    [string]$Root = "C:\dexbot",
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
Write-Host "Open http://127.0.0.1:$Port on this machine. Do not forward the port to the internet."
& $script:DexbotPython -m dexbot panel --host 127.0.0.1 --port $Port --config (Join-Path $Root "config\default.yaml")
exit $LASTEXITCODE
