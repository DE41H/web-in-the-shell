"""Error classification and operator-facing error messages.

The pipeline can fail in many places: the LLM SDK raises, httpx raises, the
dispatch layer returns 4xx/5xx, the SSRF guard blocks a navigation, the
recovery agent aborts. The operator only needs to see one structured object
per failure — an :class:`ErrorInfo` with a category, a hint, a severity, and
a flag saying whether a retry might help.

This module is the single seam between raw exceptions and operator-friendly
messages. The TUI renders :class:`ErrorInfo` directly; ``main._handle_api_error``
funnels every exception through :func:`classify`; the dispatch and recovery
layers can attach a status code without needing a real exception object.

Error categories
----------------

``AUTH``         Invalid / missing API key.
``RATE_LIMIT``   429 — too many requests, free-tier cap.
``QUOTA``        402 or "insufficient_quota" — billing issue.
``NETWORK``      No route to host, DNS, connect refused.
``TIMEOUT``      Provider or dispatch took too long.
``VALIDATION``   4xx other than 401/404/402/429 — request rejected.
``NOT_FOUND``    404.
``SERVER``       5xx — target or provider is having problems.
``BLOCKED``      SSRF guard / security policy.
``UNKNOWN``      Anything we cannot classify.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx


class ErrorCategory(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    NETWORK = "network"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    SERVER = "server"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ErrorSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_CATEGORY_ICON: dict[ErrorCategory, str] = {
    ErrorCategory.AUTH:       "KEY",
    ErrorCategory.RATE_LIMIT: "WAIT",
    ErrorCategory.QUOTA:      "PAY",
    ErrorCategory.NETWORK:    "NET",
    ErrorCategory.TIMEOUT:    "TIME",
    ErrorCategory.VALIDATION: "400",
    ErrorCategory.NOT_FOUND:  "404",
    ErrorCategory.SERVER:     "5XX",
    ErrorCategory.BLOCKED:    "BLOCK",
    ErrorCategory.UNKNOWN:    "ERR",
}

_CATEGORY_STYLE: dict[ErrorCategory, str] = {
    ErrorCategory.AUTH:       "bold red",
    ErrorCategory.RATE_LIMIT: "yellow",
    ErrorCategory.QUOTA:      "bold yellow",
    ErrorCategory.NETWORK:    "red",
    ErrorCategory.TIMEOUT:    "yellow",
    ErrorCategory.VALIDATION: "red",
    ErrorCategory.NOT_FOUND:  "dim white",
    ErrorCategory.SERVER:     "bold red",
    ErrorCategory.BLOCKED:    "bold magenta",
    ErrorCategory.UNKNOWN:    "red",
}


@dataclass(frozen=True)
class ErrorInfo:
    """Operator-facing error description.

    Attributes:
        category:    High-level bucket the failure belongs to.
        severity:    How badly this affects the run.
        title:       One-line summary safe to print to the operator.
        detail:      Longer, redacted description (truncated to 200 chars).
        hint:        What the operator can do to recover.
        retryable:   Whether a retry (or replan) is likely to help.
        status_code: HTTP status code, if the failure came from an HTTP
                     response.
        source:      Short label of where the failure originated
                     (``"planner"``, ``"executor"``, ``"dispatch"``,
                     ``"recovery"``, ``"browser"``).
    """

    category: ErrorCategory
    severity: ErrorSeverity
    title: str
    detail: str
    hint: str
    retryable: bool
    status_code: int | None = None
    source: str = ""

    @property
    def icon(self) -> str:
        return _CATEGORY_ICON.get(self.category, "ERR")

    @property
    def style(self) -> str:
        return _CATEGORY_STYLE.get(self.category, "red")

    def to_lines(self) -> list[str]:
        """Render as plain-text lines for the thought stream.

        Format::

            [AUTH] API authentication failed
              Invalid API key: sk-...
              → Run /key to re-enter your API key, or check the provider dashboard.
        """
        lines = [f"[{self.icon}] {self.title}"]
        if self.detail and self.detail != self.title:
            lines.append(f"  {self.detail}")
        if self.hint:
            lines.append(f"  → {self.hint}")
        return lines


# ---------------------------------------------------------------------------
# Per-provider billing hints — surfaced when a QUOTA / RATE_LIMIT / AUTH error
# names a known provider. Helps the operator know which dashboard to visit.
# ---------------------------------------------------------------------------

_PROVIDER_BILLING_HINT: dict[str, str] = {
    "gemini":    "Enable billing at console.cloud.google.com or use a paid API key.",
    "anthropic": "Check your Anthropic credit balance at console.anthropic.com.",
    "openai":    "Check your OpenAI billing at platform.openai.com.",
    "groq":      "Free-tier Groq limits are per-minute — wait a moment and retry.",
    "together":  "Check your Together AI usage at api.together.xyz.",
    "ollama":    "Ollama is local — check that the model is pulled and the server is running.",
}

_AUTH_HINT        = "Run /key to re-enter your API key, or check the provider dashboard."
_QUOTA_HINT       = "Your API quota is exhausted. Top up or switch provider with /provider."
_NETWORK_HINT = (
    "Check your internet connection. The app requires connectivity to the target site."
)
_TIMEOUT_HINT = (
    "Provider is slow or unreachable. Retry, or switch to a faster model with /model."
)
_RATE_LIMIT_HINT  = "Wait a moment and retry. Free tiers have per-minute limits."
_NOT_FOUND_HINT = (
    "Endpoint returned 404. The AI may have misidentified the route"
    " — try /target to override."
)
_VALIDATION_HINT  = "The API rejected the request. Inspect the captured response above."
_SERVER_HINT      = "The target server is having problems. Wait and retry, or switch target."
_BLOCKED_HINT     = "The request was blocked by the security policy (SSRF guard)."
_UNKNOWN_HINT     = "An unexpected error occurred. Re-run, or switch model with /model."


def _first_line(text: str, max_len: int = 200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:max_len]
    return ""


def _truncate(s: str, max_len: int = 200) -> str:
    s = s.strip()
    if len(s) > max_len:
        return s[:max_len]
    return s


def classify(
    exc: BaseException | None = None,
    *,
    status_code: int | None = None,
    detail: str = "",
    provider: str = "",
    source: str = "",
) -> ErrorInfo:
    """Classify an exception (or a status code) into an :class:`ErrorInfo`.

    Either ``exc`` or ``status_code`` should be supplied. When ``exc`` is
    given, its message and type name contribute to the detail string and
    category; ``status_code`` (if any) wins for HTTP-driven categories.

    Args:
        exc:         The exception to classify, or None.
        status_code: HTTP status code override (use when you have a response
                     but no exception).
        detail:      Optional operator-visible detail string. When set,
                     overrides the exception's first line.
        provider:    LLM provider name (``"anthropic"``, ``"openai"`` …) for
                     provider-specific hints.
        source:      Short label of where the failure originated.

    Returns:
        A structured :class:`ErrorInfo` safe to hand to the TUI.
    """
    if exc is not None and status_code is None and isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code

    name = type(exc).__name__ if exc is not None else ""
    msg  = str(exc) if exc is not None else ""
    combined = (msg + " " + name + " " + (detail or "")).lower()

    if detail:
        rendered_detail = _truncate(detail)
    else:
        rendered_detail = _first_line(msg) or name or (
            f"HTTP {status_code}" if status_code else "unknown error"
        )

    # ---- HTTP status-driven categories ----
    if (
        status_code == 401
        or "401" in combined
        or "invalid_api_key" in combined
        or "authenticationerror" in combined
    ):
        return ErrorInfo(
            category=ErrorCategory.AUTH,
            severity=ErrorSeverity.HIGH,
            title="API authentication failed",
            detail=rendered_detail,
            hint=_AUTH_HINT,
            retryable=False,
            status_code=status_code,
            source=source,
        )

    if (
        status_code == 429
        or "429" in combined
        or "rate_limit" in combined
        or "rate limit" in combined
        or "too many requests" in combined
    ):
        hint = _PROVIDER_BILLING_HINT.get(provider, _RATE_LIMIT_HINT)
        if "quota" in combined and provider in _PROVIDER_BILLING_HINT:
            hint = _PROVIDER_BILLING_HINT[provider]
        return ErrorInfo(
            category=ErrorCategory.RATE_LIMIT,
            severity=ErrorSeverity.MEDIUM,
            title="Rate limit hit",
            detail=rendered_detail,
            hint=hint,
            retryable=True,
            status_code=status_code,
            source=source,
        )

    if (
        status_code == 402
        or "quota" in combined
        or "insufficient_quota" in combined
        or "billing" in combined and "limit" in combined
    ):
        hint = _PROVIDER_BILLING_HINT.get(provider, _QUOTA_HINT)
        return ErrorInfo(
            category=ErrorCategory.QUOTA,
            severity=ErrorSeverity.HIGH,
            title="API quota exhausted",
            detail=rendered_detail,
            hint=hint,
            retryable=False,
            status_code=status_code,
            source=source,
        )

    if status_code == 404 or "404" in combined and "not found" in combined:
        return ErrorInfo(
            category=ErrorCategory.NOT_FOUND,
            severity=ErrorSeverity.MEDIUM,
            title="Endpoint not found (404)",
            detail=rendered_detail,
            hint=_NOT_FOUND_HINT,
            retryable=False,
            status_code=status_code,
            source=source,
        )

    if status_code is not None and 400 <= status_code < 500:
        return ErrorInfo(
            category=ErrorCategory.VALIDATION,
            severity=ErrorSeverity.MEDIUM,
            title=f"Request rejected ({status_code})",
            detail=rendered_detail,
            hint=_VALIDATION_HINT,
            retryable=False,
            status_code=status_code,
            source=source,
        )

    if status_code is not None and status_code >= 500:
        return ErrorInfo(
            category=ErrorCategory.SERVER,
            severity=ErrorSeverity.MEDIUM,
            title=f"Server error ({status_code})",
            detail=rendered_detail,
            hint=_SERVER_HINT,
            retryable=True,
            status_code=status_code,
            source=source,
        )

    # ---- Exception-type categories ----
    if (
        isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError))
        or "no connection" in combined
        or ("connect" in name.lower() and "refused" in combined)
        or "name or service not known" in combined
        or "no route to host" in combined
    ):
        return ErrorInfo(
            category=ErrorCategory.NETWORK,
            severity=ErrorSeverity.HIGH,
            title="Network error",
            detail=rendered_detail,
            hint=_NETWORK_HINT,
            retryable=True,
            source=source,
        )

    if (
        isinstance(exc, asyncio.TimeoutError)
        or isinstance(exc, TimeoutError)
        or "timed out" in combined
        or ("timeout" in combined and "read" in combined)
    ):
        return ErrorInfo(
            category=ErrorCategory.TIMEOUT,
            severity=ErrorSeverity.MEDIUM,
            title="Request timed out",
            detail=rendered_detail,
            hint=_TIMEOUT_HINT,
            retryable=True,
            source=source,
        )

    if (
        "ssrf" in combined
        or "allowlist" in combined
        or "unsafe url" in combined
        or "blocked navigation" in combined
    ):
        return ErrorInfo(
            category=ErrorCategory.BLOCKED,
            severity=ErrorSeverity.HIGH,
            title="Request blocked by policy",
            detail=rendered_detail,
            hint=_BLOCKED_HINT,
            retryable=False,
            source=source,
        )

    # ---- SDK name-based fallbacks (when we have an exc but no status code) ----
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return ErrorInfo(
            category=ErrorCategory.AUTH,
            severity=ErrorSeverity.HIGH,
            title="API authentication failed",
            detail=rendered_detail,
            hint=_AUTH_HINT,
            retryable=False,
            source=source,
        )
    if name == "RateLimitError":
        hint = _PROVIDER_BILLING_HINT.get(provider, _RATE_LIMIT_HINT)
        return ErrorInfo(
            category=ErrorCategory.RATE_LIMIT,
            severity=ErrorSeverity.MEDIUM,
            title="Rate limit hit",
            detail=rendered_detail,
            hint=hint,
            retryable=True,
            source=source,
        )
    if name in ("APIConnectionError",):
        return ErrorInfo(
            category=ErrorCategory.NETWORK,
            severity=ErrorSeverity.HIGH,
            title="Network error",
            detail=rendered_detail,
            hint=_NETWORK_HINT,
            retryable=True,
            source=source,
        )

    return ErrorInfo(
        category=ErrorCategory.UNKNOWN,
        severity=ErrorSeverity.MEDIUM,
        title=f"{name or 'Error'}: {rendered_detail[:80]}",
        detail=rendered_detail,
        hint=_UNKNOWN_HINT,
        retryable=False,
        source=source,
    )


def classify_response(response: Any, *, provider: str = "", source: str = "") -> ErrorInfo:
    """Classify an httpx.Response (or any object with .status_code/.text)."""
    status_code = getattr(response, "status_code", 0) or 0
    text        = getattr(response, "text", "") or ""
    return classify(
        exc=None,
        status_code=int(status_code) if status_code else None,
        detail=_first_line(text) if text else "",
        provider=provider,
        source=source,
    )


def severity_for_category(cat: ErrorCategory) -> ErrorSeverity:
    """Default severity for a category — used by callers that build an
    ErrorInfo without an exception.
    """
    return {
        ErrorCategory.AUTH:       ErrorSeverity.HIGH,
        ErrorCategory.RATE_LIMIT: ErrorSeverity.MEDIUM,
        ErrorCategory.QUOTA:      ErrorSeverity.HIGH,
        ErrorCategory.NETWORK:    ErrorSeverity.HIGH,
        ErrorCategory.TIMEOUT:    ErrorSeverity.MEDIUM,
        ErrorCategory.VALIDATION: ErrorSeverity.MEDIUM,
        ErrorCategory.NOT_FOUND:  ErrorSeverity.MEDIUM,
        ErrorCategory.SERVER:     ErrorSeverity.MEDIUM,
        ErrorCategory.BLOCKED:    ErrorSeverity.HIGH,
        ErrorCategory.UNKNOWN:    ErrorSeverity.MEDIUM,
    }.get(cat, ErrorSeverity.MEDIUM)


def billing_hint_for(provider: str) -> str:
    """Return the per-provider billing hint, or a generic quota hint."""
    return _PROVIDER_BILLING_HINT.get(provider, _QUOTA_HINT)
