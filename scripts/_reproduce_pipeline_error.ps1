$ErrorActionPreference = "Continue"
$root = "C:\Users\SkySnapAdmin\KompassInvest\skysnap-lead-engine"
Set-Location $root
$env:PYTHONPATH = "."
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\logs\manual_run_pipeline.log"
New-Item -ItemType Directory -Force -Path (Join-Path $root "data\logs") | Out-Null

Write-Host "=== status ==="
& $py -m skysnap status 2>&1 | Tee-Object -FilePath $log

Write-Host ""
Write-Host "=== check-config (short) ==="
& $py -m skysnap check-config 2>&1 | Tee-Object -FilePath $log -Append

Write-Host ""
Write-Host "=== run-pipeline (capturing error) ==="
& $py -m skysnap run-pipeline 2>&1 | Tee-Object -FilePath $log -Append
Write-Host "EXIT CODE: $LASTEXITCODE"
Add-Content -Path $log -Value "EXIT CODE: $LASTEXITCODE"
