# Create the venv and install Dexbot. Run from any directory.
param(
    [string]$Root = "C:\dexbot"
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
if (-not (Test-Path $Root)) {
    throw "Folder not found: $Root. Clone the repo there first."
}
Set-Location $Root

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data"), (Join-Path $Root "data\logs") | Out-Null

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $created = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -eq 0) { $created = $true }
        if (-not $created) {
            & py -3 -m venv .venv
            if ($LASTEXITCODE -eq 0) { $created = $true }
        }
    }
    if (-not $created) {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            throw "Python 3.12+ not found. Install it from https://www.python.org/downloads/windows/ and enable Add python.exe to PATH."
        }
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
    }
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $venvPython -m pip install -U pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $venvPython -m pip install -e $Root
if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed" }

$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Root ".env.example") $envFile
    Write-Host "Created $envFile. Edit it in Notepad and save as UTF-8 before the first check."
}

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    icacls $envFile /inheritance:r /grant:r "SYSTEM:(R)" "${env:USERNAME}:(R,W)" | Out-Null
    Write-Host "Limited .env to you and SYSTEM (the service account)."
} else {
    Write-Host "To lock .env down, open PowerShell as Administrator and run:"
    Write-Host "icacls `"$envFile`" /inheritance:r /grant:r `"SYSTEM:(R)`" `"${env:USERNAME}:(R,W)`""
}

Write-Host "Installed. Next: notepad $envFile"
Write-Host "Then: powershell -File `"$Root\deploy\windows\run-check.ps1`""
