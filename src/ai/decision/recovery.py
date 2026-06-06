from __future__ import annotations

import json
import re

import httpx
from dataclasses import dataclass

from security.sanitize import sanitize_for_llm
from ai.provider import LLMClient


@dataclass
class RecoveryResult:
    retry: bool
    revised_parameters: dict
    abort_reason: str | None = None


_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```")

_RECOVERY_SYSTEM = (
    "You are a recovery agent for a headless HTTP automation system. "
    "When given a failed HTTP response, diagnose the root cause and either: "
    "(a) output revised request parameters as a ```json block, or "
    "(b) output ABORT: <reason> if the failure is unrecoverable. "
    "Be terse. No prose outside of those two formats."
)


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
        original_parameters: dict,
        action: str,
        attempt_number: int = 0,
    ) -> RecoveryResult:
        try:
            error_body = sanitize_for_llm(failed_response.text[:800])
        except Exception:
            error_body = "(response body unreadable)"

        safe_params = sanitize_for_llm(json.dumps(original_parameters))

        prompt = (
            f"Attempt: {attempt_number + 1}\n"
            f"HTTP {failed_response.status_code} on action: '{action}'\n"
            f"Original parameters: {safe_params}\n"
            f"Error body:\n{error_body}"
        )

        resp = await self._client.chat(
            system=_RECOVERY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=256,
        )
        text = resp.text

        if text.strip().upper().startswith("ABORT:"):
            reason = text.split(":", 1)[1].strip() if ":" in text else text[6:].strip()
            return RecoveryResult(retry=False, revised_parameters={}, abort_reason=reason)

        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                revised = json.loads(match.group(1))
                return RecoveryResult(retry=True, revised_parameters=revised)
            except json.JSONDecodeError:
                pass

        return RecoveryResult(
            retry=False,
            revised_parameters={},
            abort_reason="Recovery agent returned unparseable output.",
        )
