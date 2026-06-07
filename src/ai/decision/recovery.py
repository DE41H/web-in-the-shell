from __future__ import annotations

import json
import re

import httpx
from dataclasses import dataclass
from typing import Any

from security.sanitize import sanitize_for_llm
from ai.provider import LLMClient


@dataclass
class RecoveryResult:
    retry: bool
    revised_parameters: dict[str, Any]
    abort_reason: str | None = None


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```")

_RECOVERY_SYSTEM = (
    "Fix a failed HTTP request. "
    "Output ONLY ONE of: "
    "(1) revised parameters as a ```json block, or "
    "(2) ABORT: <reason>. "
    "ABORT on 403 (auth/permission error), 404, or 410. "
    "No prose, no explanation outside these two formats."
)

_RECOVERY_MAX_TOKENS = 512
_ERROR_BODY_BUDGET = 600

_ABORT_PREFIXES = ("ABORT:", "ABORT ", "RETRY-ABORT:")
_BARE_JSON_OBJ_RE = re.compile(r"^\s*\{[\s\S]*\}\s*$")


class RecoveryAgent:
    """
    Reads raw HTTP error responses and instructs the pipeline to either
    retry with revised parameters or abort with a diagnosis.
    Uses a fast, cheap model — recovery calls happen in the hot path.
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def handle(
        self,
        failed_response: httpx.Response,
        original_parameters: dict[str, Any],
        action: str,
        attempt_number: int = 0,
    ) -> RecoveryResult:
        try:
            error_body = sanitize_for_llm(failed_response.text[:_ERROR_BODY_BUDGET])
        except Exception:
            error_body = "(response body unreadable)"

        safe_params = sanitize_for_llm(json.dumps(original_parameters, separators=(",", ":")))

        prompt = (
            f"Attempt: {attempt_number + 1}\n"
            f"HTTP {failed_response.status_code} on '{action}'\n"
            f"Params: {safe_params}\n"
            f"Error: {error_body}"
        )

        resp = await self._client.chat(
            system=_RECOVERY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=_RECOVERY_MAX_TOKENS,
        )
        text = (resp.text or "").strip()

        # 1) Abort branch (case-insensitive prefix, several variants).
        upper = text.upper()
        for prefix in _ABORT_PREFIXES:
            if upper.startswith(prefix):
                reason = text[len(prefix):].strip()
                if not reason:
                    reason = "Recovery agent aborted without a reason."
                return RecoveryResult(retry=False, revised_parameters={}, abort_reason=reason)

        # 2) Fenced JSON block.
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                revised = json.loads(match.group(1))
                if isinstance(revised, dict):
                    return RecoveryResult(retry=True, revised_parameters=revised)
            except json.JSONDecodeError:
                pass

        # 3) Bare JSON object.
        if _BARE_JSON_OBJ_RE.match(text):
            try:
                revised = json.loads(text)
                if isinstance(revised, dict):
                    return RecoveryResult(retry=True, revised_parameters=revised)
            except (json.JSONDecodeError, ValueError):
                pass

        return RecoveryResult(
            retry=False,
            revised_parameters={},
            abort_reason="Recovery agent returned unparseable output.",
        )
