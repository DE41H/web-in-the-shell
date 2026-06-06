from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import deque

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from network.dispatch.client import DispatchClient
from serialization.models import CompactStateModel
from ai.decision.recovery import RecoveryAgent
from ai.discovery.planner import Plan
from ai.provider import LLMClient

if TYPE_CHECKING:
    from ai.errors import ErrorInfo


_MAX_RETRIES = 3
_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```")

_EXECUTOR_SYSTEM = (
    "Construct the exact JSON body for an HTTP request. "
    "Output only a single ```json block. No prose."
)

_EXECUTOR_MAX_TOKENS = 192

_CONTEXT_BUDGET = 480


@dataclass
class ExecutionResult:
    success: bool
    endpoint: str
    status_code: int
    response_body: dict | list | None = None
    error: str | None = None
    error_info: "ErrorInfo | None" = None


class ExecutionAgent:
    """
    Refines action parameters into precise API payloads via LLM, then dispatches
    them through DispatchClient. Delegates failures to RecoveryAgent and retries
    up to _MAX_RETRIES times with revised parameters.
    """

    def __init__(
        self,
        dispatch: DispatchClient,
        client: LLMClient,
        recovery_client: LLMClient,
    ) -> None:
        self._client = client
        self._dispatch = dispatch
        self._recovery = RecoveryAgent(recovery_client)
        self.state_history: deque[CompactStateModel] = deque(maxlen=10)
        self.last_usage: dict | None = None
        self.total_usage: dict = {}

    async def execute(
        self,
        action: str,
        endpoint: str,
        parameters: dict,
        state: CompactStateModel | None = None,
        method: str = "POST",
    ) -> ExecutionResult:
        context = state.to_llm_context() if state else ""
        m = method.upper()
        # GET/DELETE never send a body — skip the LLM refinement call entirely.
        # Also skip when parameters is empty (or all values are None) — no need to
        # ask the LLM to refine nothing.
        _params_empty = not parameters or all(v is None for v in parameters.values())
        if m in ("GET", "DELETE") or _params_empty:
            payload = parameters
        else:
            payload = await self._refine_payload(action, endpoint, parameters, context)
        last_status = 0

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                if m == "GET":
                    response = await self._dispatch.get(endpoint)
                elif m == "PUT":
                    response = await self._dispatch.put(endpoint, payload)
                elif m == "PATCH":
                    response = await self._dispatch.patch(endpoint, payload)
                elif m == "DELETE":
                    delete_kwargs = {"params": payload} if payload else {}
                    response = await self._dispatch.delete(endpoint, **delete_kwargs)
                else:
                    response = await self._dispatch.post(endpoint, payload)
            except Exception as exc:
                from ai.errors import classify
                error_info = classify(exc, source="executor")
                return ExecutionResult(
                    success=False,
                    endpoint=endpoint,
                    status_code=0,
                    error=error_info.to_lines()[0] if error_info.to_lines() else str(exc),
                    error_info=error_info,
                )
            last_status = response.status_code

            if response.is_success:
                try:
                    body = response.json()
                except Exception:
                    body = None
                return ExecutionResult(
                    success=True,
                    endpoint=endpoint,
                    status_code=last_status,
                    response_body=body,
                )

            if attempt == _MAX_RETRIES:
                break

            recovery = await self._recovery.handle(
                response, payload, action, attempt_number=attempt - 1
            )

            if not recovery.retry or recovery.abort_reason:
                from ai.errors import classify
                error_info = classify(
                    exc=None,
                    status_code=last_status,
                    detail=recovery.abort_reason or f"HTTP {last_status}",
                    source="executor",
                )
                return ExecutionResult(
                    success=False,
                    endpoint=endpoint,
                    status_code=last_status,
                    error=recovery.abort_reason or f"HTTP {last_status}",
                    error_info=error_info,
                )

            payload = recovery.revised_parameters

            if response.status_code >= 500:
                await asyncio.sleep(min(2 ** attempt, 16))

        from ai.errors import classify
        error_info = classify(
            exc=None,
            status_code=last_status,
            detail=f"Exhausted {_MAX_RETRIES} retries.",
            source="executor",
        )
        return ExecutionResult(
            success=False,
            endpoint=endpoint,
            status_code=last_status,
            error=f"Exhausted {_MAX_RETRIES} retries.",
            error_info=error_info,
        )

    async def execute_plan(
        self,
        plan: Plan,
        state: CompactStateModel | None = None,
    ) -> list[ExecutionResult]:
        """
        Execute every step in plan.steps in order.  Falls back to the top-level
        plan.action / plan.target_endpoints[0] / plan.parameters when steps is empty
        (backward-compatibility with callers that build a Plan without steps).

        Stops early if any step fails and recovery cannot fix it, returning all
        results collected so far (including the failing one).

        Each step receives the most recent state from state_history so it can
        use the result of the previous step.
        """
        self.state_history.clear()
        if state is not None:
            self.state_history.append(state)

        steps = plan.steps
        if not steps:
            endpoint = plan.target_endpoints[0] if plan.target_endpoints else ""
            steps = [
                {
                    "action": plan.action,
                    "endpoint": endpoint,
                    "parameters": plan.parameters,
                }
            ]

        results: list[ExecutionResult] = []
        self.total_usage = {}
        for step_num, step in enumerate(steps, start=1):
            current_state = self.state_history[-1] if self.state_history else None
            result = await self.execute(
                action=step["action"],
                endpoint=step["endpoint"],
                parameters=step["parameters"],
                state=current_state,
                method=step.get("method", "POST"),
            )
            results.append(result)

            if self.last_usage:
                step_usage = self.last_usage
                for key, val in step_usage.items():
                    if isinstance(val, int):
                        self.total_usage[key] = self.total_usage.get(key, 0) + val
                    else:
                        self.total_usage.setdefault(key, val)
                self._log_step_cost(step_num, step_usage)

            if result.success and result.response_body is not None:
                payload = (
                    result.response_body
                    if isinstance(result.response_body, dict)
                    else {
                        "count": len(result.response_body),
                        "sample": result.response_body[0] if result.response_body else {},
                    }
                )
                self.state_history.append(CompactStateModel(
                    endpoint=result.endpoint,
                    status_code=result.status_code,
                    payload=payload,
                ))
            if not result.success:
                break

        return results

    def _log_step_cost(self, step_num: int, usage: dict) -> None:
        inp = usage.get("input", 0)
        out = usage.get("output", 0)
        model = usage.get("model", "unknown")
        print(
            f"[executor] step {step_num}: {inp} in / {out} out tokens ({model})",
            file=sys.stderr,
        )

    async def _refine_payload(
        self,
        action: str,
        endpoint: str,
        parameters: dict,
        context: str,
    ) -> dict:
        context = context[:_CONTEXT_BUDGET]
        prompt = (
            f"Action: {action}\n"
            f"Endpoint: {endpoint}\n"
            f"Params: {json.dumps(parameters)}\n"
        )
        if context:
            prompt += f"State: {context}\n"

        resp = await self._client.chat(
            system=_EXECUTOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=_EXECUTOR_MAX_TOKENS,
        )
        self.last_usage = resp.usage
        text = resp.text
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Fallback: the model may return bare JSON (no fences) — try once.
        text = text.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

        return parameters
