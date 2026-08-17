#!/usr/bin/env python3
"""Smoke-test NVIDIA NIM via the OpenAI-compatible chat completions API.

Requires:
  pip install openai python-dotenv
  NVIDIA_API_KEY=nvapi-... in .env (repo root) or environment

Usage (from repo root, venv active):
  python scripts/test_nvidia_nim.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, APIStatusError, OpenAI

# Load .env from repository root (parent of scripts/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.3-70b-instruct"
# Faster for smoke tests; large MoE models (e.g. nemotron-3-ultra-550b) can take 60-120s.
SLOW_MODEL_MARKERS = ("nemotron-3-ultra", "550b", "405b", "340b")
REQUEST_TIMEOUT_SEC = 180.0


def main() -> int:
    api_key = (os.getenv("NVIDIA_API_KEY") or "").strip() or "nvapi-REPLACE_WITH_YOUR_KEY"
    model = (os.getenv("NVIDIA_NIM_MODEL") or DEFAULT_MODEL).strip()

    if api_key.startswith("nvapi-REPLACE"):
        print(
            "ERROR: Set NVIDIA_API_KEY in .env (get one at https://build.nvidia.com/).",
            file=sys.stderr,
        )
        return 1

    client = OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SEC,
    )

    print(f"NVIDIA NIM endpoint: {NVIDIA_BASE_URL}", flush=True)
    print(f"Model: {model}", flush=True)
    if any(m in model.lower() for m in SLOW_MODEL_MARKERS):
        print(
            "Note: large models on the free tier often need 60-120s for the first reply "
            "(not frozen). For daily runs, prefer meta/llama-3.3-70b-instruct.",
            flush=True,
        )
    print(f"Sending test message (timeout {int(REQUEST_TIMEOUT_SEC)}s) ...", flush=True)

    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly one short sentence confirming you received this "
                        "connectivity test from SkySnap lead engine."
                    ),
                }
            ],
            max_tokens=128,
            temperature=0.2,
        )
    except APITimeoutError:
        elapsed = time.monotonic() - started
        print(
            f"\nERROR: Request timed out after {elapsed:.0f}s. "
            "Try NVIDIA_NIM_MODEL=meta/llama-3.3-70b-instruct in .env",
            file=sys.stderr,
        )
        return 1
    except APIStatusError as e:
        print(f"API error {e.status_code}: {e.message}", file=sys.stderr)
        return 1
    except APIConnectionError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    choice = response.choices[0].message
    text = (choice.content or "").strip()
    print(f"\n--- Response ({elapsed:.1f}s) ---", flush=True)
    print(text or "(empty content)")
    if response.usage:
        print(
            f"\nTokens — prompt: {response.usage.prompt_tokens}, "
            f"completion: {response.usage.completion_tokens}"
        )
    print("\nNVIDIA NIM connection OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
