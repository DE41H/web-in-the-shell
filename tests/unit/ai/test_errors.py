"""Tests for :mod:`ai.errors`.

Covers the full classification matrix, per-provider hints, the
:class:`ErrorInfo` renderer, and the response-driven entry point.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from ai.errors import (
    ErrorCategory,
    ErrorInfo,
    ErrorSeverity,
    billing_hint_for,
    classify,
    classify_response,
    severity_for_category,
)


# ── classify: from exception type ────────────────────────────────────────────


def test_classify_httpx_connect_error_is_network():
    exc = httpx.ConnectError("refused")
    info = classify(exc)
    assert info.category is ErrorCategory.NETWORK
    assert info.severity is ErrorSeverity.HIGH
    assert info.retryable is True
    assert "Network error" in info.title


def test_classify_httpx_network_error_is_network():
    info = classify(httpx.NetworkError("dns"))
    assert info.category is ErrorCategory.NETWORK


def test_classify_plain_connection_error_is_network():
    info = classify(ConnectionError("no route to host"))
    assert info.category is ErrorCategory.NETWORK


def test_classify_asyncio_timeout_error_is_timeout():
    info = classify(asyncio.TimeoutError())
    assert info.category is ErrorCategory.TIMEOUT
    assert info.retryable is True


def test_classify_builtin_timeout_error_is_timeout():
    info = classify(TimeoutError("read timeout"))
    assert info.category is ErrorCategory.TIMEOUT


def test_classify_authentication_error_name_is_auth():
    class AuthenticationError(Exception):
        pass

    info = classify(AuthenticationError("bad key"))
    assert info.category is ErrorCategory.AUTH
    assert info.severity is ErrorSeverity.HIGH
    assert info.retryable is False


def test_classify_rate_limit_error_name_is_rate_limit():
    class RateLimitError(Exception):
        pass

    info = classify(RateLimitError("429"))
    assert info.category is ErrorCategory.RATE_LIMIT
    assert info.retryable is True


def test_classify_api_connection_error_name_is_network():
    class APIConnectionError(Exception):
        pass

    info = classify(APIConnectionError("conn"))
    assert info.category is ErrorCategory.NETWORK


def test_classify_unknown_exception_falls_back_to_unknown():
    info = classify(ValueError("weird thing"))
    assert info.category is ErrorCategory.UNKNOWN
    assert info.retryable is False
    assert info.title.startswith("ValueError")


def test_classify_none_exception_with_substring_429_is_rate_limit():
    info = classify(None, detail="got 429 from upstream", source="dispatch")
    assert info.category is ErrorCategory.RATE_LIMIT


def test_classify_none_exception_with_substring_quota_is_quota():
    info = classify(None, detail="insufficient_quota — top up first")
    assert info.category is ErrorCategory.QUOTA
    assert info.retryable is False


def test_classify_none_exception_with_substring_invalid_api_key_is_auth():
    info = classify(None, detail="invalid_api_key on request")
    assert info.category is ErrorCategory.AUTH


# ── classify: from status_code ───────────────────────────────────────────────


def test_classify_status_401_is_auth():
    info = classify(None, status_code=401, detail="unauthorized")
    assert info.category is ErrorCategory.AUTH
    assert info.status_code == 401


def test_classify_status_402_is_quota():
    info = classify(None, status_code=402, detail="payment required")
    assert info.category is ErrorCategory.QUOTA


def test_classify_status_404_is_not_found():
    info = classify(None, status_code=404, detail="not found")
    assert info.category is ErrorCategory.NOT_FOUND
    assert info.retryable is False


def test_classify_status_429_is_rate_limit():
    info = classify(None, status_code=429, detail="too many requests")
    assert info.category is ErrorCategory.RATE_LIMIT


def test_classify_status_400_is_validation():
    info = classify(None, status_code=400, detail="bad json")
    assert info.category is ErrorCategory.VALIDATION


def test_classify_status_422_is_validation():
    info = classify(None, status_code=422, detail="unprocessable")
    assert info.category is ErrorCategory.VALIDATION


def test_classify_status_500_is_server():
    info = classify(None, status_code=500, detail="internal error")
    assert info.category is ErrorCategory.SERVER
    assert info.retryable is True


def test_classify_status_503_is_server():
    info = classify(None, status_code=503, detail="unavailable")
    assert info.category is ErrorCategory.SERVER


# ── classify: blocking / SSRF substrings ─────────────────────────────────────


def test_classify_ssrf_in_detail_is_blocked():
    info = classify(None, detail="ssrf guard rejected localhost", source="browser")
    assert info.category is ErrorCategory.BLOCKED
    assert info.retryable is False


def test_classify_allowlist_in_detail_is_blocked():
    info = classify(None, detail="allowlist denied example.com")
    assert info.category is ErrorCategory.BLOCKED


def test_classify_unsafe_url_value_error_is_blocked():
    info = classify(ValueError("unsafe url: 169.254.169.254"))
    assert info.category is ErrorCategory.BLOCKED


# ── classify: per-provider hints ─────────────────────────────────────────────


def test_classify_rate_limit_gemini_hint():
    info = classify(None, status_code=429, provider="gemini", detail="rate limit")
    assert "console.cloud.google.com" in info.hint


def test_classify_rate_limit_anthropic_hint():
    info = classify(None, status_code=429, provider="anthropic", detail="rate limit")
    assert "console.anthropic.com" in info.hint


def test_classify_rate_limit_openai_hint():
    info = classify(None, status_code=429, provider="openai", detail="rate limit")
    assert "platform.openai.com" in info.hint


def test_classify_rate_limit_groq_hint():
    info = classify(None, status_code=429, provider="groq", detail="rate limit")
    assert "per-minute" in info.hint


def test_classify_rate_limit_ollama_hint():
    info = classify(None, status_code=429, provider="ollama", detail="rate limit")
    assert "Ollama is local" in info.hint


def test_classify_quota_anthropic_hint():
    info = classify(None, status_code=402, provider="anthropic", detail="quota")
    assert "console.anthropic.com" in info.hint


def test_classify_unknown_provider_uses_generic_hint():
    info = classify(None, status_code=429, provider="made-up-co", detail="rate")
    assert info.hint
    assert "console" not in info.hint
    assert "platform.openai.com" not in info.hint


# ── classify: status_code pulled from httpx.HTTPStatusError ─────────────────


def test_classify_pulls_status_from_httpx_http_status_error():
    response = MagicMock()
    response.status_code = 503
    exc = httpx.HTTPStatusError("boom", request=MagicMock(), response=response)
    info = classify(exc, detail="bad gateway")
    assert info.category is ErrorCategory.SERVER
    assert info.status_code == 503


# ── classify: source / detail / truncation ───────────────────────────────────


def test_classify_passes_source_through():
    info = classify(None, status_code=500, source="dispatch")
    assert info.source == "dispatch"


def test_classify_passes_provider_when_no_quota_phrase():
    info = classify(None, status_code=429, provider="anthropic", detail="rate")
    assert info.source == ""  # source default


def test_classify_truncates_very_long_detail():
    long = "x" * 5000
    info = classify(None, status_code=500, detail=long)
    assert len(info.detail) <= 200


def test_classify_uses_first_nonempty_line_of_message():
    info = classify(RuntimeError("first line\nsecond line\nthird"))
    assert info.detail.startswith("first line")


def test_classify_uses_first_line_when_status_code_present_but_no_msg():
    info = classify(None, status_code=500, detail="  spaced detail  ")
    assert info.detail == "spaced detail"


# ── ErrorInfo.to_lines ──────────────────────────────────────────────────────


def test_to_lines_minimal_when_detail_equals_title():
    info = ErrorInfo(
        category=ErrorCategory.NETWORK,
        severity=ErrorSeverity.HIGH,
        title="Network error",
        detail="Network error",
        hint="",
        retryable=True,
    )
    lines = info.to_lines()
    assert lines == ["[NET] Network error"]


def test_to_lines_includes_detail_when_distinct():
    info = ErrorInfo(
        category=ErrorCategory.AUTH,
        severity=ErrorSeverity.HIGH,
        title="API authentication failed",
        detail="Invalid API key: sk-...",
        hint="Check the dashboard",
        retryable=False,
    )
    lines = info.to_lines()
    assert lines[0] == "[KEY] API authentication failed"
    assert "Invalid API key" in lines[1]
    assert "Check the dashboard" in lines[2]


def test_to_lines_omits_hint_when_empty():
    info = ErrorInfo(
        category=ErrorCategory.NETWORK,
        severity=ErrorSeverity.HIGH,
        title="x",
        detail="y",
        hint="",
        retryable=True,
    )
    lines = info.to_lines()
    assert len(lines) == 2
    assert lines[0] == "[NET] x"
    assert lines[1] == "  y"


def test_icon_for_each_category_is_distinct():
    seen: dict[str, ErrorCategory] = {}
    for cat in ErrorCategory:
        info = ErrorInfo(
            category=cat,
            severity=ErrorSeverity.MEDIUM,
            title="t",
            detail="d",
            hint="h",
            retryable=False,
        )
        assert info.icon
        seen.setdefault(info.icon, cat)
    assert len(seen) == len(ErrorCategory)


def test_style_returns_nonempty_string():
    info = ErrorInfo(
        category=ErrorCategory.NETWORK,
        severity=ErrorSeverity.HIGH,
        title="t",
        detail="d",
        hint="h",
        retryable=True,
    )
    assert isinstance(info.style, str)
    assert info.style


# ── classify_response ──────────────────────────────────────────────────────


def test_classify_response_500_uses_status_code():
    response = MagicMock()
    response.status_code = 500
    response.text = "internal error"
    info = classify_response(response, source="dispatch")
    assert info.category is ErrorCategory.SERVER
    assert info.source == "dispatch"


def test_classify_response_429_uses_provider_hint():
    response = MagicMock()
    response.status_code = 429
    response.text = "rate limit"
    info = classify_response(response, provider="anthropic")
    assert info.category is ErrorCategory.RATE_LIMIT
    assert "console.anthropic.com" in info.hint


def test_classify_response_handles_empty_text():
    response = MagicMock()
    response.status_code = 502
    response.text = ""
    info = classify_response(response)
    assert info.category is ErrorCategory.SERVER


def test_classify_response_handles_no_status_attr():
    obj = MagicMock(spec=[])
    info = classify_response(obj)
    assert info.category is ErrorCategory.UNKNOWN


# ── severity_for_category / billing_hint_for ───────────────────────────────


def test_severity_for_category_auth_is_high():
    assert severity_for_category(ErrorCategory.AUTH) is ErrorSeverity.HIGH


def test_severity_for_category_rate_limit_is_medium():
    assert severity_for_category(ErrorCategory.RATE_LIMIT) is ErrorSeverity.MEDIUM


def test_severity_for_category_blocked_is_high():
    assert severity_for_category(ErrorCategory.BLOCKED) is ErrorSeverity.HIGH


def test_severity_for_category_unknown_falls_back_to_medium():
    assert severity_for_category(ErrorCategory.UNKNOWN) is ErrorSeverity.MEDIUM


def test_billing_hint_for_known_provider():
    h = billing_hint_for("anthropic")
    assert "console.anthropic.com" in h


def test_billing_hint_for_unknown_provider_uses_generic_quota():
    h = billing_hint_for("made-up-co")
    assert "quota" in h.lower() or "switch" in h.lower()


# ── parens: priority of 401 over a generic message ──────────────────────────


def test_classify_anthropic_authentication_error_with_status_401():
    class AuthenticationError(Exception):
        pass

    info = classify(AuthenticationError("nope"), status_code=401)
    assert info.category is ErrorCategory.AUTH
    assert info.status_code == 401


# ── ensure all enum members are exercised by to_lines ────────────────────────


@pytest.mark.parametrize("cat", list(ErrorCategory))
def test_every_category_renders_to_lines(cat: ErrorCategory):
    info = ErrorInfo(
        category=cat,
        severity=ErrorSeverity.MEDIUM,
        title="t",
        detail="d",
        hint="h",
        retryable=False,
    )
    lines = info.to_lines()
    assert lines
    assert lines[0].startswith(f"[{info.icon}]")
