# Register Dexbot to start at boot and restart after a crash.
# Prefers NSSM. Falls back to Task Scheduler. Run in an elevated PowerShell.
param(
    [string]$Root = "C:\dexbot",
    [string]$NssmPath = ""
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Open PowerShell as Administrator and run this script again."
}

. (Join-Path $PSScriptRoot "_common.ps1")
Use-DexbotEnv -Root $Root
$python = $script:DexbotPython
$config = Join-Path $Root "config\default.yaml"

if (-not $NssmPath) {
    if (Get-Command nssm -ErrorAction SilentlyContinue) {
        $NssmPath = (Get-Command nssm).Source
    } elseif (Test-Path "C:\nssm\win64\nssm.exe") {
        $NssmPath = "C:\nssm\win64\nssm.exe"
    } elseif (Test-Path "C:\nssm\nssm.exe") {
        $NssmPath = "C:\nssm\nssm.exe"
    }
}

$envBlock = @(
    "PYTHONUTF8=1",
    "PYTHONIOENCODING=utf-8",
    "DEXBOT_JOURNAL=$($env:DEXBOT_JOURNAL)",
    "DEXBOT_KILL_FILE=$($env:DEXBOT_KILL_FILE)",
    "DEXBOT_LOG_FILE=$($env:DEXBOT_LOG_FILE)"
) -join "`n"

if ($NssmPath -and (Test-Path $NssmPath)) {
    Write-Host "Using NSSM: $NssmPath"
    & $NssmPath stop Dexbot confirm 2>$null
    & $NssmPath remove Dexbot confirm 2>$null
    & $NssmPath install Dexbot $python "-m" "dexbot" "run" "--config" $config
    if ($LASTEXITCODE -ne 0) { throw "nssm install failed" }
    & $NssmPath set Dexbot AppDirectory $Root
    & $NssmPath set Dexbot AppEnvironmentExtra $envBlock
    & $NssmPath set Dexbot AppStdout (Join-Path $Root "data\logs\service-stdout.log")
    & $NssmPath set Dexbot AppStderr (Join-Path $Root "data\logs\service-stderr.log")
    & $NssmPath set Dexbot AppRotateFiles 1
    & $NssmPath set Dexbot AppExit Default Restart
    & $NssmPath set Dexbot AppRestartDelay 15000
    & $NssmPath set Dexbot Start SERVICE_AUTO_START
    & $NssmPath start Dexbot
    if ($LASTEXITCODE -ne 0) { throw "nssm start failed" }
    Write-Host "Service Dexbot is installed and started. Logs: $Root\data\logs"
    Write-Host "Status: Get-Service Dexbot"
    return
}

Write-Host "NSSM not found. Registering scheduled task Dexbot (at startup, restart on failure)."
Write-Host "To use NSSM instead, download it from https://nssm.cc/download and re-run with -NssmPath."

$argument = "-m dexbot run --config `"$config`""
$action = New-ScheduledTaskAction -Execute $python -Argument $argument -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
try {
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
} catch {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
}
Register-ScheduledTask -TaskName "Dexbot" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "Dexbot"
Write-Host "Task Dexbot is registered and started."
Write-Host "Status: Get-ScheduledTask -TaskName Dexbot"
Write-Host "The task process does not read extra environment variables. Keep the working directory at $Root so data\journal.sqlite and data\KILL resolve, or put absolute DEXBOT_* paths in .env."
