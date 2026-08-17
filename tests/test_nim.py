"""Tests for NVIDIA NIM fallback helpers."""

from skysnap.nim import is_anthropic_unavailable_error


def test_usage_limit_error_is_fallback_eligible():
    err = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'You have reached your specified API usage limits. "
        "You will regain access on 2026-07-01 at 00:00 UTC.'}}"
    )
    assert is_anthropic_unavailable_error(err)


def test_auth_error_is_not_fallback_eligible():
    import httpx
    from anthropic import AuthenticationError

    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    assert not is_anthropic_unavailable_error(
        AuthenticationError("invalid x-api-key", response=response, body=None)
    )
