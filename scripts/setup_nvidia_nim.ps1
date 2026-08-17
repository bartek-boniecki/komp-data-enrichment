# Setup NVIDIA NIM (OpenAI-compatible) in the project venv (Windows).
# Run from repo root:  powershell -File scripts/setup_nvidia_nim.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtual environment at .venv ..."
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install openai python-dotenv

$envFile = ".env"
if (-not (Test-Path $envFile)) {
    if (Test-Path .env.example) {
        Copy-Item .env.example $envFile
        Write-Host "Created $envFile from .env.example"
    } else {
        New-Item -ItemType File -Path $envFile | Out-Null
    }
}

$content = Get-Content $envFile -Raw -ErrorAction SilentlyContinue
if ($content -match '(?m)^NVIDIA_API_KEY=') {
    Write-Host "NVIDIA_API_KEY already present in $envFile (not overwritten)."
} else {
    @"

# --- NVIDIA NIM (OpenAI-compatible fallback) ---
# Create at https://build.nvidia.com/ → API catalog → Get API Key
NVIDIA_API_KEY=nvapi-REPLACE_WITH_YOUR_KEY
# NVIDIA_NIM_MODEL=meta/llama-3.3-70b-instruct
"@ | Add-Content -Path $envFile -Encoding utf8
    Write-Host "Appended NVIDIA_API_KEY placeholder to $envFile — edit with your nvapi-... key."
}

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1. Edit .env and set NVIDIA_API_KEY=nvapi-..."
Write-Host "  2. .\.venv\Scripts\Activate.ps1"
Write-Host "  3. python scripts/test_nvidia_nim.py"
