# Register (or update) a Windows Scheduled Task that runs the full SkySnap pipeline
# every day at 20:00 local time.
#
# Run from an elevated PowerShell (recommended) or as the user that should own the job:
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_pipeline.ps1
#
# Optional:
#   -TaskName "SkySnap-Lead-Pipeline"
#   -At "20:00"
#   -PythonExe "C:\path\to\.venv\Scripts\python.exe"

param(
    [string]$TaskName = "SkySnap-Lead-Pipeline",
    [string]$At = "20:00",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $PythonExe) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonExe = $venvPython
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) {
            throw "No Python found. Create .venv in the repo or pass -PythonExe."
        }
        $PythonExe = $cmd.Source
        Write-Warning "Using system Python ($PythonExe). Prefer a project .venv on the VM."
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

$logsDir = Join-Path $RepoRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m skysnap run-pipeline" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "SkySnap full pipeline: ingest-email + run-daily + HubSpot push" `
    -Force | Out-Null

Write-Host "Scheduled task registered."
Write-Host "  Name:      $TaskName"
Write-Host "  When:      daily at $At (machine local time)"
Write-Host "  Python:    $PythonExe"
Write-Host "  Start in:  $RepoRoot"
Write-Host "  Command:   -m skysnap run-pipeline"
Write-Host ""
Write-Host "Verify:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Run now: Start-ScheduledTask -TaskName '$TaskName'"
