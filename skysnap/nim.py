"""NVIDIA NIM (OpenAI-compatible) inference fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_NIM_MODEL = "meta/llama-3.3-70b-instruct"


def is_anthropic_unavailable_error(exc: BaseException) -> bool:
    """True when Anthropic is temporarily unusable and NIM fallback is appropriate."""
    from anthropic import AuthenticationError

    if isinstance(exc, AuthenticationError):
        return False
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "usage limits",
            "credit balance",
            "insufficient",
            "billing",
            "overloaded",
            "rate limit",
            "rate_limit",
            "too many requests",
        )
    )


def make_nim_client(*, api_key: str, timeout_sec: float = 180.0) -> OpenAI:
    from openai import OpenAI

    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=api_key,
        timeout=timeout_sec,
    )


def nim_ping(*, api_key: str, model: str) -> str:
    """Raise on failure; return assistant text on success."""
    client = make_nim_client(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=8,
        temperature=0.0,
    )
    return (response.choices[0].message.content or "").strip()
