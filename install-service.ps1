# One-command setup (Windows): install the Clip agent bridge as a Scheduled Task
# that starts at logon, runs hidden (pythonw), and restarts on failure.
#
#   powershell -ExecutionPolicy Bypass -File .\install-service.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-service.ps1 -CaptureDir "$HOME\clips"
#   powershell -ExecutionPolicy Bypass -File .\install-service.ps1 -Uninstall
#
# Needs Python 3.11+ from python.org (the "py" launcher). No admin required.

param(
    [switch]$Uninstall,
    [string]$CaptureDir = ""
)

$ErrorActionPreference = "Stop"
$BridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName  = "ClipBridge"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "clip-bridge task removed."
    exit 0
}

# --- venv -------------------------------------------------------------------
$Python  = Join-Path $BridgeDir ".venv\Scripts\python.exe"
$PythonW = Join-Path $BridgeDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Setting up venv (first run)..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv (Join-Path $BridgeDir ".venv")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv (Join-Path $BridgeDir ".venv")
    } else {
        Write-Error "Python not found - install it from python.org first."
    }
    & $Python -m pip install -q -e $BridgeDir
}

# --- scheduled task ---------------------------------------------------------
# pythonw = no console window. -m server resolves via the working directory.
$Arg = "-m server"
if ($CaptureDir -ne "") { $Arg = "$Arg --capture-dir `"$CaptureDir`"" }
$Action   = New-ScheduledTaskAction -Execute $PythonW -Argument $Arg -WorkingDirectory $BridgeDir
$Trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# --- verify -----------------------------------------------------------------
Start-Sleep -Seconds 3
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:7777/health" -TimeoutSec 3
    Write-Host "OK: clip-bridge running on http://localhost:7777 (MCP at /mcp)"
    Write-Host "    starts at logon, restarts on failure (Task Scheduler: '$TaskName')"
    Write-Host "    status: Get-ScheduledTaskInfo -TaskName $TaskName"
} catch {
    Write-Warning "Task installed but health check failed."
    Write-Warning "Try running it in a console to see the error:"
    Write-Warning "  $Python -m server   (from $BridgeDir)"
    exit 1
}
