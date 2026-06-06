from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator

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


def _format_value(v: Any, max_len: int = 120) -> str:
    """Format a payload value for LLM context output.

    Dicts and lists are serialised as JSON so the LLM receives valid JSON
    rather than Python repr notation.
    """
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:max_len]
    return str(v)[:max_len]


class CompactStateModel(BaseModel):
    """
    Minimal representation of a single API response for LLM consumption.
    Strips UI/telemetry noise at construction time so the token footprint
    reflects only semantically meaningful fields.
    """

    model_config = ConfigDict(extra="ignore", strict=False)

    endpoint: str
    status_code: int
    payload: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def strip_noise(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = data.get("payload", {})
        if isinstance(payload, dict):
            data["payload"] = _deep_strip(payload, _NOISE_KEYS)
        return data

    def to_llm_context(self) -> str:
        path = urlparse(self.endpoint).path or self.endpoint
        lines = [f"{path} → {self.status_code}"]
        for k, v in list(self.payload.items())[:8]:
            lines.append(f"{k}={_format_value(v)}")
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
