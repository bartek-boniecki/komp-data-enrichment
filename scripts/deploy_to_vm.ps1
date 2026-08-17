<#
.SYNOPSIS
    Copy SkySnap code to the VM without touching its database or config.

.DESCRIPTION
    'scp -r' on the whole project folder also overwrites data\skysnap.sqlite and
    .env on the VM, destroying its leads, export snapshots and HubSpot links.
    This script copies only the code and docs, leaving VM-local state alone.

    Never copied: data\ (database), .env (VM credentials), .venv\, .git\

    Note: scp copies and overwrites, it does not mirror. Files deleted locally
    stay on the VM until removed there.

.EXAMPLE
    .\scripts\deploy_to_vm.ps1

.EXAMPLE
    .\scripts\deploy_to_vm.ps1 -DryRun
#>
param(
    [string]$SshTarget = "skysnapagent",
    [string]$RemoteRoot = "C:\Users\SkySnapAdmin\KompassInvest\skysnap-lead-engine",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$items = @(
    "skysnap",
    "tests",
    "docs",
    "scripts",
    "requirements.txt",
    "README.md",
    ".env.example"
)

Write-Host "Deploying to ${SshTarget}:${RemoteRoot}" -ForegroundColor Cyan
Write-Host "Excluded: data\, .env, .venv\, .git\" -ForegroundColor DarkGray

foreach ($item in $items) {
    $source = Join-Path $projectRoot $item
    if (-not (Test-Path $source)) {
        Write-Host "  skip    $item (not found)" -ForegroundColor DarkGray
        continue
    }
    $scpArgs = @()
    if (Test-Path $source -PathType Container) {
        $scpArgs += "-r"
    }
    $scpArgs += $source
    $scpArgs += "${SshTarget}:`"$RemoteRoot`""

    if ($DryRun) {
        Write-Host "  would   scp $($scpArgs -join ' ')" -ForegroundColor Yellow
        continue
    }

    Write-Host "  sending $item"
    & scp @scpArgs
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed for '$item' (exit $LASTEXITCODE)"
    }
}

if (-not $DryRun) {
    Write-Host "Done. On the VM run: python -m skysnap status" -ForegroundColor Green
}
