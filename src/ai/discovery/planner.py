from __future__ import annotations

import json
import re
import uuid
import httpx
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from security.sanitize import sanitize_for_llm
from ai.provider import LLMClient
from persistence import ConvoStore


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai(self) -> dict[str, Any]:
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
    parameters: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    candidate_domains: list[str] = field(default_factory=list)


def _normalize_domain(raw: str) -> str:
    """Canonicalize a domain string returned by the LLM.

    Adds https:// when the scheme is missing, strips any path the LLM
    accidentally put in the domain field, and lowercases the hostname.
    """
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    p = urlparse(raw)
    scheme = p.scheme.lower() or "https"
    # Keep only scheme + netloc (drop spurious paths / query strings)
    netloc = p.netloc.lower() if p.netloc else p.path.lower().split("/")[0]
    return f"{scheme}://{netloc}"


# --- Tool definitions (provider-agnostic) ---

_TOOL_ROUTE_TO_DOMAIN = ToolSchema(
    name="route_to_domain",
    description="Route to a known domain and specify endpoints and action.",
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": (
                    "Primary base URL of the target application (scheme + host only, "
                    "no path). Example: https://api.github.com"
                ),
                "pattern": "^https?://",
            },
            "candidate_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Alternative base URLs to probe if the primary domain is "
                    "unreachable: www vs non-www variants, regional subdomains, "
                    "mobile endpoints. Omit when confident."
                ),
            },
            "endpoints": {
                "type": "array",
                "items": {"type": "string", "pattern": "^/"},
                "description": "API endpoint paths to intercept. Each must start with '/'.",
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
                "pattern": "^https?://",
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
                            "pattern": "^/",
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

# Map tool name -> ToolSchema for quick lookup during validation
_TOOL_BY_NAME: dict[str, ToolSchema] = {t.name: t for t in _PLANNER_TOOLS}


def _validate_against_schema(schema: dict, data: Any, path: str = "") -> None:
    """Validate the given data against the provided JSON Schema using jsonschema.

    This function requires jsonschema to be available as a runtime dependency.
    Validation errors are re-raised as ValueError so the planner keeps its
    existing exception contracts for callers and tests.
    """
    from jsonschema import validate, ValidationError as _JSVError

    try:
        validate(instance=data, schema=schema)
    except _JSVError as e:
        raise ValueError(str(e)) from e

_SYSTEM = (
    "Routing agent: map a user's intent to the real production domain, API endpoints, "
    "action, and parameters.\n"
    "Rules:\n"
    "• domain must be scheme + host only — no path (e.g. https://api.github.com, "
    "https://www.reddit.com). Never example.com, localhost, or search engines.\n"
    "• Include www. only when the canonical site requires it. Prefer API subdomains "
    "when the task is programmatic (e.g. api.twitter.com over twitter.com).\n"
    "• If uncertain between two plausible domains (e.g. www variant vs bare, "
    "regional subdomain), add 1-2 alternatives in candidate_domains.\n"
    "• If the target domain is completely unknown, call fallback_search.\n"
    "• Use plan_steps for goals requiring more than one sequential API call.\n"
    "• Output only tool calls — no prose."
)

_FALLBACK_SYSTEM = (
    "Discovery agent for a headless AI browser. Given a search query and optional "
    "search results, identify multiple relevant production domains and their "
    "API structures, then call route_to_domain.\n"
    "• When the user intent implies a list or multiple results (e.g., 'top 3 pubs', 'list all users'), "
    "prioritize identifying all relevant API endpoints for retrieving such lists.\n"
    "• domain must be scheme + host only (e.g. https://www.example.com) — no path.\n"
    "• Include candidate_domains with www/non-www or regional alternatives when "
    "uncertain.\n"
    "• Output only a tool call — no prose."
)

_FALLBACK_TOOLS = [_TOOL_ROUTE_TO_DOMAIN]

_MAX_HISTORY_MESSAGES = 12
_PLANNER_MAX_TOKENS = 384
_FALLBACK_MAX_TOKENS = 384

# Simple in-memory plan cache to reduce repeated LLM calls for identical intents.
# Keyed by (intent, context_snippet). Size bounded to avoid memory growth.
# NOTE: plan caching is instance-scoped (PlannerAgent._plan_cache) to avoid
# cross-test/process interference during unit tests.


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
        self.last_usage: dict[str, Any] | None = None
        self.last_messages: list[dict[str, Any]] = []
        self._plan_cache: dict[tuple[str, str], Plan] = {}
        self._plan_cache_max = 64

    async def plan(self, user_intent: str, context: str = "") -> Plan:
        safe_intent = sanitize_for_llm(user_intent)
        safe_context = sanitize_for_llm(context) if context else ""
        messages: list[dict[str, Any]] = []

        # Check cache first (quick path). Cache key includes a short context
        # snippet so different failure contexts produce new plans.
        cache_key = (safe_intent, safe_context[:200])
        if cache_key in self._plan_cache:
            return self._plan_cache[cache_key]

        if self._convos is not None:
            past = await self._convos.get_latest_for_intent(safe_intent)
            if past is not None:
                # Cap replayed history to the last N messages (~6 turns) — keep
                # token cost bounded as the local store grows.
                messages.extend(past.to_llm_messages()[-_MAX_HISTORY_MESSAGES:])

        # M1: only include context label when context is non-empty
        content = f"Intent: {safe_intent}"
        if safe_context:
            content += f"\n\nCurrent state context:\n{safe_context}"
        messages.append({"role": "user", "content": content})

        try:
            resp = await self._client.chat(
                system=_SYSTEM,
                messages=messages,
                tools=_PLANNER_TOOLS,
                max_tokens=_PLANNER_MAX_TOKENS,
            )
        except Exception as exc:
            # If the provider reported a tool/function failure, attempt discovery
            # via fallback_search immediately instead of failing the pipeline.
            txt = str(exc).lower()
            if (
                "failed to call a function" in txt
                or "tool_use_failed" in txt
                or "failed_generation" in txt
            ):
                # Ask the fallback handler to try discovery using the intent
                return await self.handle_fallback(safe_intent, messages=messages)
            raise
        self.last_usage = resp.usage

        if resp.text:
            messages.append({"role": "assistant", "content": resp.text})

        # H2: append tool-call/tool-result pairs BEFORE saving last_messages.
        # Format is provider-specific: Anthropic uses content-block arrays,
        # OpenAI-compatible providers (Gemini, Groq, etc.) use tool_calls + role=tool.
        if resp.tool_calls:
            if self._client.provider == "anthropic":
                tc_ids = [tc.id or str(uuid.uuid4()) for tc in resp.tool_calls]
                messages.append({
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": tc_ids[i], "name": tc.name, "input": tc.input}
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_ids[i],
                            "content": json.dumps(tc.input),
                        }
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
            else:
                # OpenAI / Gemini / Groq format
                tc_ids = [str(uuid.uuid4()) for _ in resp.tool_calls]
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc_ids[i],
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
                for i, tc in enumerate(resp.tool_calls):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_ids[i],
                        "content": json.dumps(tc.input),
                    })

        self.last_messages = list(messages)

        for call in resp.tool_calls:
            # Validate tool input against its ToolSchema when possible to
            # catch malformed tool outputs before using them.
            schema = _TOOL_BY_NAME.get(call.name)
            if schema is not None:
                try:
                    _validate_against_schema(schema.parameters, call.input)
                except ValueError as e:
                    # Preserve existing test-friendly error messages for known tools
                    if call.name == "route_to_domain":
                        raise ValueError("Malformed route_to_domain") from e
                    if call.name == "plan_steps":
                        raise ValueError("Malformed plan_steps") from e
                    raise ValueError(f"Tool call '{call.name}' failed validation: {e}") from e

            if call.name == "route_to_domain":
                inp = call.input
                try:
                    # Validate required keys are present; raise ValueError for malformed tool calls
                    for req in ("domain", "endpoints", "action", "parameters"):
                        if req not in inp:
                            raise ValueError(
                                f"Malformed route_to_domain tool call — missing key {req}"
                            )
                    # Normalise domain + endpoints
                    domain = _normalize_domain(inp["domain"])
                    candidates = [
                        _normalize_domain(d)
                        for d in inp.get("candidate_domains", [])
                        if isinstance(d, str) and d.strip()
                    ]
                    endpoints = [
                        (e if e.startswith("/") else "/" + e)[:200]
                        for e in inp.get("endpoints", [])
                    ]
                    first_endpoint = endpoints[0] if endpoints else ""
                    steps = [
                        {
                            "action": inp["action"],
                            "endpoint": first_endpoint,
                            "parameters": inp["parameters"],
                            "method": inp.get("method", "POST"),
                        }
                    ]
                    plan = Plan(
                        target_domain=domain,
                        target_endpoints=endpoints,
                        action=inp["action"],
                        parameters=inp["parameters"],
                        steps=steps,
                        candidate_domains=candidates,
                    )
                    # Cache plan per-instance
                    self._plan_cache[cache_key] = plan
                    if len(self._plan_cache) > self._plan_cache_max:
                        self._plan_cache.pop(next(iter(self._plan_cache)))
                    return plan
                except KeyError as e:
                    raise ValueError(
                        f"Malformed route_to_domain tool call — missing key {e}"
                    ) from e

            if call.name == "plan_steps":
                inp = call.input
                try:
                    raw_steps: list[dict[str, Any]] = inp.get("steps", [])
                    if not raw_steps:
                        raise ValueError("plan_steps tool call returned an empty steps list.")
                    # Normalise each step: apply "POST" default when method is absent
                    steps = [
                        {**s, "method": s.get("method", "POST")}
                        for s in raw_steps
                    ]
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

        raise ValueError(
            f"Planner produced no actionable tool call. "
            f"text={resp.text[:120]!r} tool_calls={[c.name for c in resp.tool_calls]}"
        )

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

    async def handle_fallback(
        self, query: str, *, messages: list[dict[str, Any]] | None = None
    ) -> Plan:
        """
        Performs a real DuckDuckGo search for the query, then uses an LLM call
        to discover the most likely domain + API endpoint structure and return
        a concrete Plan.

        H1: receives the primary call's message list for history storage.
        The fallback LLM call itself only receives text-only messages — tool-call
        history (role=tool, assistant with tool_calls, Anthropic tool_use blocks)
        is stripped before sending because the fallback uses a different tool set
        (_FALLBACK_TOOLS = [route_to_domain] only). Sending history that references
        fallback_search (not in _FALLBACK_TOOLS) causes providers like Gemini to
        produce 0 output tokens.
        """
        search_results = await self._search_duckduckgo(query)

        content = f"Search query: {sanitize_for_llm(query)}\n\n"
        if search_results:
            content += f"Search results:\n{sanitize_for_llm(search_results)}\n\n"
        else:
            content += (
                "No search results were found. "
                "Use your knowledge to infer the best domain and API structure.\n\n"
            )
        content += (
            "Based on the above, identify all likely web application domains "
            "and API endpoints needed to fulfil the intent, especially if the intent "
            "implies a list of results (e.g., 'top 3 pubs'). Call route_to_domain."        )

        # H1: keep the full message list for history storage
        history: list[dict[str, Any]] = list(messages) if messages else []

        # Build a clean message list for the LLM call: strip tool-call messages
        # (role=tool, assistant.tool_calls, Anthropic content-list blocks) so the
        # fallback model doesn't see references to tools outside _FALLBACK_TOOLS.
        def _is_tool_message(m: dict[str, Any]) -> bool:
            if m.get("role") == "tool":
                return True
            if m.get("role") == "assistant" and m.get("tool_calls"):
                return True
            content = m.get("content")
            if isinstance(content, list):
                return any(
                    isinstance(c, dict) and c.get("type") in ("tool_use", "tool_result")
                    for c in content
                )
            return False

        llm_messages = [m for m in history if not _is_tool_message(m)]
        llm_messages.append({"role": "user", "content": content})

        resp = await self._client.chat(
            system=_FALLBACK_SYSTEM,
            messages=llm_messages,
            tools=_FALLBACK_TOOLS,
            max_tokens=_FALLBACK_MAX_TOKENS,
        )
        self.last_usage = resp.usage

        # Append to full history for storage (not to llm_messages)
        history.append({"role": "user", "content": content})
        if resp.text:
            history.append({"role": "assistant", "content": resp.text})

        if resp.tool_calls:
            if self._client.provider == "anthropic":
                tc_ids = [tc.id or str(uuid.uuid4()) for tc in resp.tool_calls]
                history.append({
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": tc_ids[i], "name": tc.name, "input": tc.input}
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
                history.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_ids[i],
                            "content": json.dumps(tc.input),
                        }
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
            else:
                tc_ids = [str(uuid.uuid4()) for _ in resp.tool_calls]
                history.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tc_ids[i],
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
                for i, tc in enumerate(resp.tool_calls):
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc_ids[i],
                        "content": json.dumps(tc.input),
                    })

        self.last_messages = list(history)

        for call in resp.tool_calls:
            if call.name != "route_to_domain":
                continue

            inp = call.input
            try:
                if "endpoints" not in inp:
                    raise KeyError("endpoints")
                domain = _normalize_domain(inp["domain"])
                candidates = [
                    _normalize_domain(d)
                    for d in inp.get("candidate_domains", [])
                    if isinstance(d, str) and d.strip()
                ]
                endpoints = inp.get("endpoints", [])
                endpoints = [
                    (e if e.startswith("/") else "/" + e)[:200] for e in endpoints
                ]
                first_endpoint = endpoints[0] if endpoints else ""
                steps = [
                    {
                        "action": inp["action"],
                        "endpoint": first_endpoint,
                        "parameters": inp["parameters"],
                        "method": inp.get("method", "POST"),
                    }
                ]
                return Plan(
                    target_domain=domain,
                    target_endpoints=endpoints,
                    action=inp["action"],
                    parameters=inp["parameters"],
                    steps=steps,
                    candidate_domains=candidates,
                )
            except KeyError as e:
                raise ValueError(
                    f"Malformed route_to_domain tool call in fallback — missing key {e}"
                ) from e

        raise ValueError(
            f"Fallback planner produced no route_to_domain tool call. "
            f"text={resp.text[:120]!r} tool_calls={[c.name for c in resp.tool_calls]}"
        )
