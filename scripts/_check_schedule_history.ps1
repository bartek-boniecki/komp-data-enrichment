# Inspect SkySnap scheduled task history on the VM
$ErrorActionPreference = "Continue"
$TaskName = "SkySnap-Lead-Pipeline"

Write-Host "=== Scheduled task ==="
schtasks /Query /TN $TaskName /V /FO LIST

Write-Host ""
Write-Host "=== Get-ScheduledTaskInfo ==="
try {
    Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State, TaskPath
    Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
} catch {
    Write-Host $_
}

Write-Host ""
Write-Host "=== Task Scheduler Operational log (14 days, SkySnap) ==="
try {
    $events = Get-WinEvent -FilterHashtable @{
        LogName = "Microsoft-Windows-TaskScheduler/Operational"
        StartTime = (Get-Date).AddDays(-14)
    } -ErrorAction Stop |
        Where-Object { $_.Message -match "SkySnap-Lead-Pipeline" } |
        Select-Object -First 50

    if ($null -eq $events -or $events.Count -eq 0) {
        Write-Host "(no matching events - history may be disabled)"
    } else {
        foreach ($e in $events) {
            $msg = $e.Message -replace "`r", " " -replace "`n", " | "
            if ($msg.Length -gt 220) { $msg = $msg.Substring(0, 220) + "..." }
            $ts = $e.TimeCreated.ToString("yyyy-MM-dd HH:mm:ss")
            Write-Host "$ts  id=$($e.Id)  $($e.LevelDisplayName)  $msg"
        }
    }
} catch {
    Write-Host "Event log read failed: $_"
}

Write-Host ""
Write-Host "=== data\logs (newest first) ==="
$logDir = "C:\Users\SkySnapAdmin\KompassInvest\skysnap-lead-engine\data\logs"
if (Test-Path $logDir) {
    Get-ChildItem $logDir -Recurse -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 25 |
        ForEach-Object {
            $ts = $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
            Write-Host "$ts  $($_.Length)  $($_.FullName)"
        }
} else {
    Write-Host "missing: $logDir"
}

Write-Host ""
Write-Host "=== Recent files under data\ ==="
Get-ChildItem "C:\Users\SkySnapAdmin\KompassInvest\skysnap-lead-engine\data" -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 15 |
    ForEach-Object {
        $ts = $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        Write-Host "$ts  $($_.Length)  $($_.Name)"
    }
