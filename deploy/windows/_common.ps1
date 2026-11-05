# Shared paths for the Windows scripts. Not run on its own.
$ErrorActionPreference = "Stop"

function Use-DexbotEnv {
    param([string]$Root = "C:\dexbot")
    if (-not (Test-Path $Root)) {
        throw "Folder not found: $Root"
    }
    Set-Location $Root
    $script:DexbotPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $script:DexbotPython)) {
        throw "Missing $script:DexbotPython. Run deploy\windows\install.ps1 first."
    }
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    if (-not $env:DEXBOT_JOURNAL) {
        $env:DEXBOT_JOURNAL = Join-Path $Root "data\journal.sqlite"
    }
    if (-not $env:DEXBOT_KILL_FILE) {
        $env:DEXBOT_KILL_FILE = Join-Path $Root "data\KILL"
    }
    if (-not $env:DEXBOT_LOG_FILE) {
        $env:DEXBOT_LOG_FILE = Join-Path $Root "data\logs\dexbot.log"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $env:DEXBOT_JOURNAL), (Split-Path $env:DEXBOT_LOG_FILE) | Out-Null
}
