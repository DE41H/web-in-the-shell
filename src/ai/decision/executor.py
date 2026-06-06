import asyncio
import json
import re
from collections import deque

from dataclasses import dataclass

from network.dispatch.client import DispatchClient
from serialization.models import CompactStateModel
from ai.decision.recovery import RecoveryAgent
from ai.discovery.planner import Plan
from ai.provider import LLMClient


_MAX_RETRIES = 3
_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```")

_EXECUTOR_SYSTEM = (
    "Construct the exact JSON body for an HTTP request. Output only a single ```json block."
)


@dataclass
class ExecutionResult:
    success: bool
    endpoint: str
    status_code: int
    response_body: dict | list | None = None
    error: str | None = None


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

    async def execute(
        self,
        action: str,
        endpoint: str,
        parameters: dict,
        state: CompactStateModel | None = None,
        method: str = "POST",
    ) -> ExecutionResult:
        context = state.to_llm_context() if state else ""
        # GET requests never send a body — skip the LLM refinement call entirely
        if method.upper() == "GET":
            payload = parameters
        else:
            payload = await self._refine_payload(action, endpoint, parameters, context)
        last_status = 0

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                m = method.upper()
                if m == "GET":
                    response = await self._dispatch.get(endpoint)
                elif m == "PUT":
                    response = await self._dispatch.put(endpoint, payload)
                elif m == "PATCH":
                    response = await self._dispatch.patch(endpoint, payload)
                else:
                    response = await self._dispatch.post(endpoint, payload)
            except Exception as exc:
                return ExecutionResult(
                    success=False,
                    endpoint=endpoint,
                    status_code=0,
                    error=f"Dispatch error ({type(exc).__name__}): {exc}",
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

            recovery = await self._recovery.handle(response, payload, action)

            if not recovery.retry or recovery.abort_reason:
                return ExecutionResult(
                    success=False,
                    endpoint=endpoint,
                    status_code=last_status,
                    error=recovery.abort_reason or f"HTTP {last_status}",
                )

            payload = recovery.revised_parameters

            if response.status_code == 429 or response.status_code >= 500:
                await asyncio.sleep(min(2 ** attempt, 16))

        return ExecutionResult(
            success=False,
            endpoint=endpoint,
            status_code=last_status,
            error=f"Exhausted {_MAX_RETRIES} retries.",
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
            # Backward-compat: treat the top-level fields as a single step
            endpoint = plan.target_endpoints[0] if plan.target_endpoints else ""
            steps = [
                {
                    "action": plan.action,
                    "endpoint": endpoint,
                    "parameters": plan.parameters,
                }
            ]

        results: list[ExecutionResult] = []
        for step in steps:
            current_state = self.state_history[-1] if self.state_history else None
            result = await self.execute(
                action=step["action"],
                endpoint=step["endpoint"],
                parameters=step["parameters"],
                state=current_state,
                method=step.get("method", "POST"),
            )
            results.append(result)
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
                # Stop processing further steps; return partial results
                break

        return results

    async def _refine_payload(
        self,
        action: str,
        endpoint: str,
        parameters: dict,
        context: str,
    ) -> dict:
        context = context[:800]
        prompt = (
            f"Action: {action}\n"
            f"Endpoint: {endpoint}\n"
            f"Suggested parameters: {json.dumps(parameters)}\n"
            f"Current application state:\n{context}"
        ).strip()

        resp = await self._client.chat(
            system=_EXECUTOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=256,
        )
        self.last_usage = resp.usage
        text = resp.text
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        return parameters
