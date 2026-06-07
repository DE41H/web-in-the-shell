from __future__ import annotations

import json
from typing import TYPE_CHECKING

from security.sanitize import sanitize_for_llm
from ai.provider import LLMClient

if TYPE_CHECKING:
    from ai.decision.executor import ExecutionResult


_ANSWER_SYSTEM = (
    "Given a user intent and API response data, determine if the goal was achieved.\n"
    "If yes: output ANSWER: <concise 1-2 sentence answer using data from the response>.\n"
    "If no (empty result, matchType=NONE, null/missing data): output RETRY: <why not met>.\n"
    "No prose outside these two formats."
)

_MAX_TOKENS = 256
_BODY_BUDGET = 800


class AnswerAgent:
    """
    Runs after a successful HTTP response to extract a human-readable answer
    and verify the goal was actually met.  Uses the cheap/fast recovery model.
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def synthesize(
        self,
        intent: str,
        results: list[ExecutionResult],
    ) -> tuple[bool, str]:
        """Return (goal_satisfied, answer_or_reason).

        goal_satisfied=False forces a replan even when HTTP was 200 (e.g. GBIF
        matchType=NONE).  goal_satisfied=True signals the pipeline to stop and
        display the answer text.
        """
        body_parts: list[str] = []
        for r in results:
            if r.response_body is not None:
                body_parts.append(
                    sanitize_for_llm(json.dumps(r.response_body)[:_BODY_BUDGET])
                )

        prompt = f"Intent: {sanitize_for_llm(intent)}\n\nResponse data:\n"
        if body_parts:
            prompt += "\n---\n".join(body_parts)
        else:
            prompt += "(no response body)"

        try:
            resp = await self._client.chat(
                system=_ANSWER_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=_MAX_TOKENS,
            )
        except Exception:
            # If the answer check itself fails, optimistically trust HTTP 200.
            return True, "Request completed successfully."

        text = (resp.text or "").strip()
        upper = text.upper()

        if upper.startswith("ANSWER:"):
            return True, text[7:].strip()
        if upper.startswith("RETRY:"):
            return False, text[6:].strip()
        # Neither prefix — model returned prose or an unexpected format.
        # Trust the HTTP success rather than blocking the pipeline.
        return True, text or "Request completed successfully."
