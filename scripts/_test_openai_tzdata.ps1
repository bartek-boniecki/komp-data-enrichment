$ErrorActionPreference = "Stop"
$root = "C:\Users\SkySnapAdmin\KompassInvest\skysnap-lead-engine"
Set-Location $root
$env:PYTHONPATH = "."
$py = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "=== import smoke ==="
& $py -c "import openai; from zoneinfo import ZoneInfo; ZoneInfo('Europe/Warsaw'); print('imports_ok', openai.__version__)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "=== ClaudeClient / NIM init ==="
& $py -c @"
from skysnap.config import load_settings
from skysnap.engine import _make_claude
s = load_settings()
c = _make_claude(s, command='test')
print('claude_ok', type(c).__name__, 'nim', c._nvidia_client is not None)
"@
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "=== check-config ==="
& $py -m skysnap check-config
$code = $LASTEXITCODE
Write-Host "check-config exit: $code"

Write-Host ""
Write-Host "=== run-pipeline dry smoke (ingest only path start) ==="
# Full pipeline can take long; verify it gets past ClaudeClient init.
& $py -c @"
from skysnap.config import load_settings
from skysnap.engine import ingest_from_email
s = load_settings()
# dry-ish: mark_seen False, just ensure no ModuleNotFoundError at start
print('calling ingest_from_email...')
try:
    res = ingest_from_email(s, mark_seen=False)
    print('ingest_ok', {k: res.get(k) for k in ('ingested','skipped','errors','leads_created') if k in res or True})
    print('ingest_keys', sorted(res.keys())[:20])
    print('ingest_summary', res)
except Exception as e:
    print('ingest_failed', type(e).__name__, e)
    raise
"@
Write-Host "ingest exit: $LASTEXITCODE"
exit $LASTEXITCODE
