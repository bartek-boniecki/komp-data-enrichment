#!/usr/bin/env bash
# Setup NVIDIA NIM (OpenAI-compatible) fallback client in the project venv.
# Run from repo root:  bash scripts/setup_nvidia_nim.sh
#
# Windows (PowerShell) equivalent:
#   .\.venv\Scripts\Activate.ps1
#   python -m pip install --upgrade pip
#   pip install openai python-dotenv
#   # then add NVIDIA_API_KEY=nvapi-... to .env manually

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment at .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
pip install openai python-dotenv

ENV_FILE=".env"
EXAMPLE=".env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$EXAMPLE" ]]; then
    cp "$EXAMPLE" "$ENV_FILE"
    echo "Created $ENV_FILE from $EXAMPLE"
  else
    touch "$ENV_FILE"
    echo "Created empty $ENV_FILE"
  fi
fi

if grep -q '^NVIDIA_API_KEY=' "$ENV_FILE" 2>/dev/null; then
  echo "NVIDIA_API_KEY already present in $ENV_FILE (not overwritten)."
else
  cat >> "$ENV_FILE" <<'EOF'

# --- NVIDIA NIM (OpenAI-compatible fallback) ---
# Create at https://build.nvidia.com/ → API catalog → Get API Key
NVIDIA_API_KEY=nvapi-REPLACE_WITH_YOUR_KEY
# Optional model override (default in test script: meta/llama-3.3-70b-instruct)
# NVIDIA_NIM_MODEL=meta/llama-3.3-70b-instruct
EOF
  echo "Appended NVIDIA_API_KEY placeholder to $ENV_FILE — edit with your nvapi-... key."
fi

echo ""
echo "Done. Next steps:"
echo "  1. Edit .env and set NVIDIA_API_KEY=nvapi-..."
echo "  2. source .venv/bin/activate"
echo "  3. python scripts/test_nvidia_nim.py"
