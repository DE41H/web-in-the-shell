from __future__ import annotations

import json
import uuid
import httpx
from dataclasses import dataclass, field

from security.sanitize import sanitize_for_llm
from ai.provider import LLMClient
from persistence import ConvoStore


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema object

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Plan:
    target_domain: str
    target_endpoints: list[str]
    action: str
    parameters: dict
    # Multi-step support: each step is {"action": str, "endpoint": str, "parameters": dict}
    # The top-level action/target_endpoints[0]/parameters represent the first step for
    # backward compatibility with main.py.
    steps: list[dict] = field(default_factory=list)


# --- Tool definitions (provider-agnostic) ---

_TOOL_ROUTE_TO_DOMAIN = ToolSchema(
    name="route_to_domain",
    description="Route to a known domain and specify endpoints and action.",
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Full base URL of the target application.",
            },
            "endpoints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "API endpoint paths to intercept.",
            },
            "action": {
                "type": "string",
                "description": "Short label for the intended action.",
            },
            "parameters": {
                "type": "object",
                "description": "Key-value pairs for the action's initial parameters.",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "description": "HTTP method for this action. Defaults to POST.",
            },
        },
        "required": ["domain", "endpoints", "action", "parameters"],
    },
)

_TOOL_FALLBACK_SEARCH = ToolSchema(
    name="fallback_search",
    description="Search for the target domain when API structure is unknown.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to discover the target application.",
            },
        },
        "required": ["query"],
    },
)

_TOOL_PLAN_STEPS = ToolSchema(
    name="plan_steps",
    description="Create a multi-step plan requiring more than one API call.",
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Full base URL of the target application.",
            },
            "steps": {
                "type": "array",
                "description": "Ordered list of API calls to execute.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "Short label for this step.",
                        },
                        "endpoint": {
                            "type": "string",
                            "description": "API endpoint path for this step.",
                        },
                        "parameters": {
                            "type": "object",
                            "description": "Key-value pairs for this step's request body.",
                        },
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                            "description": "HTTP method for this step. Defaults to POST.",
                        },
                    },
                    "required": ["action", "endpoint", "parameters"],
                },
            },
        },
        "required": ["domain", "steps"],
    },
)

_PLANNER_TOOLS = [_TOOL_ROUTE_TO_DOMAIN, _TOOL_FALLBACK_SEARCH, _TOOL_PLAN_STEPS]

_SYSTEM = (
    "You are a routing agent for a headless protocol AI. "
    "Given a user's intent, determine: the target web application domain, "
    "which API endpoints to intercept, and the exact action + parameters to execute. "
    "Use plan_steps when the goal needs more than one API call. "
    "Output only tool calls — no prose."
)

_FALLBACK_SYSTEM = (
    "You are a discovery agent for a headless protocol AI. "
    "Given a search query describing an application the user wants to automate, "
    "identify the most likely domain and API endpoint structure, then call route_to_domain. "
    "Output only a tool call — no prose."
)

# Tools available during fallback (no plan_steps, no recursive fallback_search)
_FALLBACK_TOOLS = [_TOOL_ROUTE_TO_DOMAIN]


class PlannerAgent:
    """Maps user intent to a concrete execution plan via LLM tool calling.

    When constructed with a `convos: ConvoStore`, the planner loads the
    most-recent past conversation for the same intent and prepends it to its
    LLM message stream, giving the model short-term memory across runs. The
    full message stream sent to the LLM is exposed as `last_messages` so the
    caller can persist it.
    """

    def __init__(self, client: LLMClient, convos: ConvoStore | None = None) -> None:
        self._client = client
        self._convos = convos
        self.last_usage: dict | None = None
        self.last_messages: list[dict] = []

    async def plan(self, user_intent: str, context: str = "") -> Plan:
        safe_intent = sanitize_for_llm(user_intent)
        safe_context = sanitize_for_llm(context) if context else ""
        messages: list[dict] = []

        if self._convos is not None:
            past = await self._convos.get_latest_for_intent(safe_intent)
            if past is not None:
                # Cap replayed conversation history to the last 20 messages (10 turns)
                messages.extend(past.to_llm_messages()[-20:])

        # M1: only include context label when context is non-empty
        content = f"Intent: {safe_intent}"
        if safe_context:
            content += f"\n\nCurrent state context:\n{safe_context}"
        messages.append({"role": "user", "content": content})

        resp = await self._client.chat(
            system=_SYSTEM,
            messages=messages,
            tools=_PLANNER_TOOLS,
            max_tokens=512,
        )
        self.last_usage = resp.usage

        if resp.text:
            messages.append({"role": "assistant", "content": resp.text})

        # H2: append tool-call/tool-result pairs BEFORE saving last_messages
        if resp.tool_calls:
            tool_use_content = [
                {
                    "type": "tool_use",
                    "id": tc.id or str(uuid.uuid4()),
                    "name": tc.name,
                    "input": tc.input,
                }
                for tc in resp.tool_calls
            ]
            messages.append({"role": "assistant", "content": tool_use_content})

            tool_result_content = [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_content[i]["id"],
                    "content": json.dumps(tc.input),
                }
                for i, tc in enumerate(resp.tool_calls)
            ]
            messages.append({"role": "user", "content": tool_result_content})

        self.last_messages = list(messages)

        for call in resp.tool_calls:
            if call.name == "route_to_domain":
                inp = call.input
                try:
                    first_endpoint = inp["endpoints"][0] if inp["endpoints"] else ""
                    steps = [
                        {
                            "action": inp["action"],
                            "endpoint": first_endpoint,
                            "parameters": inp["parameters"],
                            "method": inp.get("method", "POST"),
                        }
                    ]
                    return Plan(
                        target_domain=inp["domain"],
                        target_endpoints=inp["endpoints"],
                        action=inp["action"],
                        parameters=inp["parameters"],
                        steps=steps,
                    )
                except KeyError as e:
                    raise ValueError(
                        f"Malformed route_to_domain tool call — missing key {e}"
                    ) from e

            if call.name == "plan_steps":
                inp = call.input
                try:
                    steps: list[dict] = inp.get("steps", [])
                    if not steps:
                        raise ValueError("plan_steps tool call returned an empty steps list.")
                    first = steps[0]
                    return Plan(
                        target_domain=inp["domain"],
                        target_endpoints=[s["endpoint"] for s in steps],
                        action=first["action"],
                        parameters=first["parameters"],
                        steps=steps,
                    )
                except KeyError as e:
                    raise ValueError(f"Malformed plan_steps tool call — missing key {e}") from e

            if call.name == "fallback_search":
                return await self.handle_fallback(call.input["query"], messages=messages)

        raise ValueError(f"Planner produced no actionable tool call. Response: {resp}")

    async def _search_duckduckgo(self, query: str) -> str:
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.get("https://api.duckduckgo.com/", params=params)
                data = r.json()
            except Exception:
                return ""
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                parts.append(topic["Text"])
        return "\n".join(parts)[:1500]

    async def handle_fallback(self, query: str, *, messages: list[dict] | None = None) -> Plan:
        """
        Performs a real DuckDuckGo search for the query, then uses an LLM call
        to discover the most likely domain + API endpoint structure and return
        a concrete Plan.

        H1: receives the primary call's message list and APPENDS to it rather
        than replacing it, so full conversation history is preserved.
        """
        search_results = await self._search_duckduckgo(query)

        content = f"Search query: {sanitize_for_llm(query)}\n\n"
        if search_results:
            content += f"Search results:\n{sanitize_for_llm(search_results)}\n\n"
        content += (
            "Based on the above, identify the most likely web application domain "
            "and API endpoints needed to fulfil the intent. Call route_to_domain."
        )

        # H1: extend the existing message list rather than starting fresh
        if messages is None:
            messages = []
        messages.append({"role": "user", "content": content})

        resp = await self._client.chat(
            system=_FALLBACK_SYSTEM,
            messages=messages,
            tools=_FALLBACK_TOOLS,
            max_tokens=512,
        )
        self.last_usage = resp.usage

        if resp.text:
            messages.append({"role": "assistant", "content": resp.text})

        self.last_messages = list(messages)

        for call in resp.tool_calls:
            if call.name != "route_to_domain":
                continue

            inp = call.input
            try:
                first_endpoint = inp["endpoints"][0] if inp["endpoints"] else ""
                steps = [
                    {
                        "action": inp["action"],
                        "endpoint": first_endpoint,
                        "parameters": inp["parameters"],
                        "method": inp.get("method", "POST"),
                    }
                ]
                return Plan(
                    target_domain=inp["domain"],
                    target_endpoints=inp["endpoints"],
                    action=inp["action"],
                    parameters=inp["parameters"],
                    steps=steps,
                )
            except KeyError as e:
                raise ValueError(
                    f"Malformed route_to_domain tool call in fallback — missing key {e}"
                ) from e

        raise ValueError(
            f"Fallback planner produced no route_to_domain tool call. Response: {resp}"
        )
