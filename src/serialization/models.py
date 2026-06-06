from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator, Field

from network.intercept.sniffer import CapturedResponse
from security.sanitize import sanitize_for_llm


# Keys that appear in virtually every API response but carry zero semantic signal
_NOISE_KEYS: frozenset[str] = frozenset({
    "tracking", "analytics", "telemetry", "metadata", "debug", "diagnostics",
    "__typename", "_links", "_embedded", "links", "meta", "pagination_meta",
    "ui_state", "layout", "styles", "className", "testId", "data-testid",
    "impression_id", "session_id", "request_id", "trace_id", "span_id",
    "experiment", "variant", "ab_test", "feature_flags",
    "__v", "createdAt", "updatedAt", "deletedAt", "created_at", "updated_at",
    "deleted_at", "cursor", "next_cursor", "page_token", "scroll_id", "etag",
    "rate_limit", "quota", "cache_key", "ttl", "expires_at", "x_request_id",
    "timestamp", "nonce", "checksum",
})


def _deep_strip(obj: dict[str, Any], noise: frozenset[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in obj.items():
        if k in noise:
            continue
        if isinstance(v, dict):
            result[k] = _deep_strip(v, noise)
        elif isinstance(v, list):
            result[k] = [
                _deep_strip(item, noise) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _format_value(v: Any, max_len: int = 80) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:max_len]
    return str(v)[:max_len]


class CompactStateModel(BaseModel):
    """Compact, validated representation of a single API response for LLM use.

    Enforces types/limits and strips common noisy keys to keep token
    consumption predictable. The LLM-facing context should remain small
    (few keys, short values).
    """

    # Keep backward-compatible behavior for tests: accept extra fields but ignore them.
    model_config = ConfigDict(extra="ignore", strict=False)

    endpoint: str = Field(..., max_length=200)
    status_code: int = Field(..., ge=100, le=599)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def strip_noise(cls, data: Any) -> Any:
        # Accept CapturedResponse-like objects by duck-typing
        if hasattr(data, "url") and hasattr(data, "status"):
            # construct from capture
            return {
                "endpoint": getattr(data, "url"),
                "status_code": getattr(data, "status"),
                "payload": getattr(data, "parsed_json", {}) or {},
            }

        if not isinstance(data, dict):
            # Preserve previous behaviour: pass-through non-mapping inputs so
            # callers can run the validator in isolation in tests.
            return data

        payload = data.get("payload", {})
        if isinstance(payload, dict):
            # shallow copy after noise strip to preserve original keys
            data["payload"] = _deep_strip(payload, _NOISE_KEYS)
        return data

    def to_llm_context(self) -> str:
        # Use only path for brevity; fall back to endpoint if parsing fails
        try:
            path = urlparse(self.endpoint).path or self.endpoint
        except Exception:
            path = self.endpoint
        lines = [f"{path} → {self.status_code}"]
        # Keep only a small number of keys and short values
        for k, v in list(self.payload.items())[:6]:
            lines.append(f"{k}={_format_value(v, max_len=120)}")
        return sanitize_for_llm("\n".join(lines))

    @property
    def compact_size(self) -> int:
        return len(self.to_llm_context().encode())


def compact_from_capture(capture: CapturedResponse) -> CompactStateModel:
    payload: dict[str, Any]
    if isinstance(capture.json, dict):
        payload = capture.json
    elif isinstance(capture.json, list):
        # Summarize list responses — pass count + first item as sample
        payload = {"count": len(capture.json), "sample": capture.json[0] if capture.json else {}}
    else:
        payload = {}

    return CompactStateModel(
        endpoint=capture.url,
        status_code=capture.status,
        payload=payload,
    )
