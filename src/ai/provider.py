"""Provider-agnostic async LLM client.

Supports Anthropic (Claude) natively and any OpenAI-compatible endpoint
(OpenAI, Groq, Google Gemini, Together AI, local Ollama) via the openai SDK.

Usage:
    client = LLMClient("anthropic", api_key="sk-ant-...", model="claude-sonnet-4-6")
    response = await client.chat(system=..., messages=..., tools=[...], max_tokens=512)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from pydantic import BaseModel

import httpx

_TEXT_TOOL_RE = re.compile(r'\b([A-Za-z_]\w*)\s*>\s*(\{)')


DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-4o",
    "groq":      "llama-3.3-70b-versatile",
    "gemini":    "gemini-2.0-flash",
    "together":  "meta-llama/Llama-3-70b-chat-hf",
    "ollama":    "llama3.2",
    "openrouter": "openai/gpt-4o",
}

DEFAULT_RECOVERY_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "groq":      "llama-3.1-8b-instant",
    "gemini":    "gemini-2.0-flash",
    "together":  "meta-llama/Llama-3-8b-chat-hf",
    "ollama":    "llama3.2",
}

_OPENAI_BASE_URLS: dict[str, str | None] = {
    "openai":   None,
    "groq":     "https://api.groq.com/openai/v1",
    "gemini":   "https://generativelanguage.googleapis.com/v1beta/openai/",
    "together": "https://api.together.xyz/v1",
    "ollama":   "http://localhost:11434/v1",
}

PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "together":  "TOGETHER_API_KEY",
    "ollama":    "",
}

_REQUEST_TIMEOUT_S: float = 60.0


class ToolCall(BaseModel):
    name: str
    input: dict[str, Any] = {}
    id: str = ""


class LLMResponse(BaseModel):
    tool_calls: list[ToolCall] = []
    text: str = ""
    usage: dict[str, Any] = {}
    stop_reason: str = ""


class ToolCallFailed(Exception):
    """Raised when the provider reports a tool/function call failure."""


def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Extract tool calls embedded as plain text by small/quantized models.

    Some models (e.g. llama-3.1-8b-instant on Groq) ignore the OpenAI
    tool_calls wire format and instead emit calls like:

        tool_name>{"arg": "val"}<function
        next_tool>{"arg": "val"}

    This parser finds every ``word>{`` pattern, extracts the full JSON
    object with a bracket-depth counter, and returns ToolCall objects.
    Unknown tool names are left to the planner's own validation.
    """
    calls: list[ToolCall] = []
    for m in _TEXT_TOOL_RE.finditer(text):
        name = m.group(1)
        start = m.start(2)  # position of the opening '{'
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        json_str = text[start : i + 1] if depth == 0 else text[start:]
        try:
            args = json.loads(json_str)
            if isinstance(args, dict):
                calls.append(ToolCall(name=name, input=args))
        except (json.JSONDecodeError, ValueError):
            pass
    return calls


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    """Best-effort coercion of a tool-call `arguments` field to a dict.

    Providers (and our own mocks) sometimes return ``arguments`` as:
      * a JSON string (OpenAI spec, most providers)
      * a dict (already parsed — some adapters)
      * ``None`` / empty string (no args)
    Any other shape, or invalid JSON, collapses to ``{}`` — a recoverable
    fallback rather than a hard error.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logging.warning("_coerce_tool_args: invalid JSON in tool arguments: %r", raw)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coerce_usage(usage: Any) -> dict[str, Any]:
    """Normalise Anthropic and OpenAI usage objects to {input, output, model}."""
    if usage is None:
        return {"input": 0, "output": 0, "model": ""}
    inp = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
    out = (
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", 0)
        or 0
    )
    return {"input": int(inp or 0), "output": int(out or 0), "model": ""}


class LLMClient:
    """Unified async LLM client. One instance per provider + model combination.

    `tools` passed to `chat()` must be `ToolSchema` objects from planner.py —
    each provider renders them in its own format internally.
    """

    def __init__(self, provider: str, api_key: str, model: str | None = None) -> None:
        self.provider = provider.lower()
        self.model    = model or DEFAULT_MODELS.get(self.provider, "gpt-4o")
        self.api_key  = api_key

        if self.provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                raise ImportError("Install anthropic: uv add anthropic")
            self._backend = anthropic.AsyncAnthropic(api_key=api_key)
            self._kind    = "anthropic"
        elif self.provider in _OPENAI_BASE_URLS:
            try:
                import openai
            except ImportError:
                raise ImportError("Install openai: uv add openai")
            self._backend = openai.AsyncOpenAI(
                api_key=api_key or "ollama",
                base_url=_OPENAI_BASE_URLS[self.provider],
            )
            self._kind = "openai"
        else:
            raise ValueError(
                f"Unknown provider {self.provider!r}. "
                f"Choose from: {', '.join(DEFAULT_MODELS)}"
            )

    async def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[Any],
        max_tokens: int,
    ) -> LLMResponse:
        if self._kind == "anthropic":
            return await self._chat_anthropic(system, messages, tools, max_tokens)
        return await self._chat_openai(system, messages, tools, max_tokens)

    # ------------------------------------------------------------------
    # Provider backends
    # ------------------------------------------------------------------

    async def _chat_anthropic(
        self, system: str, messages: list[dict[str, Any]], tools: list[Any], max_tokens: int
    ) -> LLMResponse:
        rendered = [t.to_anthropic() for t in tools]
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if rendered:
            kwargs["tools"] = rendered

        try:
            resp = await asyncio.wait_for(
                self._backend.messages.create(**kwargs),  # type: ignore[attr-defined]
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError:
            raise RuntimeError(
                f"LLM API timed out after {_REQUEST_TIMEOUT_S:.0f}s "
                f"({self.provider}/{self.model})."
            )
        except Exception as exc:
            import anthropic as _anthropic
            # Provider connectivity issues
            if isinstance(exc, (_anthropic.APIConnectionError, httpx.ConnectError,
                                httpx.NetworkError)):
                raise RuntimeError(f"No connection to {self.provider} API: {exc}") from exc
            # Detect provider tool/function call failure messages and raise a dedicated exception
            txt = str(exc).lower()
            if (
                "tool_use_failed" in txt
                or "failed to call a function" in txt
                or "failed_generation" in txt
            ):
                from .provider import ToolCallFailed
                raise ToolCallFailed(str(exc)) from exc
            raise

        calls: list[ToolCall] = []
        text = ""
        for block in (resp.content or []):
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                calls.append(ToolCall(
                    name=getattr(block, "name", "") or "",
                    input=getattr(block, "input", None) or {},
                    id=getattr(block, "id", "") or "",
                ))
            elif btype == "text":
                text = getattr(block, "text", "") or ""

        usage = _coerce_usage(getattr(resp, "usage", None))
        usage["model"] = self.model
        return LLMResponse(
            tool_calls=calls,
            text=text,
            usage=usage,
            stop_reason=str(getattr(resp, "stop_reason", "") or ""),
        )

    async def _chat_openai(
        self, system: str, messages: list[dict[str, Any]], tools: list[Any], max_tokens: int
    ) -> LLMResponse:
        oai_messages = [{"role": "system", "content": system}] + list(messages or [])
        rendered = [t.to_openai() for t in tools]

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=oai_messages,
        )
        if rendered:
            kwargs["tools"] = rendered
            kwargs["tool_choice"] = "auto"

        try:
            resp = await asyncio.wait_for(
                self._backend.chat.completions.create(**kwargs),  # type: ignore[attr-defined]
                timeout=_REQUEST_TIMEOUT_S,
            )
        except TimeoutError:
            raise RuntimeError(
                f"LLM API timed out after {_REQUEST_TIMEOUT_S:.0f}s "
                f"({self.provider}/{self.model})."
            )
        except Exception as exc:
            import openai as _openai
            if isinstance(exc, (_openai.APIConnectionError, httpx.ConnectError,
                                httpx.NetworkError)):
                raise RuntimeError(f"No connection to {self.provider} API: {exc}") from exc
            txt = str(exc).lower()
            if (
                "tool_use_failed" in txt
                or "failed to call a function" in txt
                or "failed_generation" in txt
            ):
                from .provider import ToolCallFailed
                raise ToolCallFailed(str(exc)) from exc
            raise

        if not getattr(resp, "choices", None):
            return LLMResponse(
                tool_calls=[], text="",
                usage={"input": 0, "output": 0, "model": self.model},
                stop_reason="",
            )

        msg = resp.choices[0].message

        calls: list[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            calls.append(ToolCall(
                name=getattr(getattr(tc, "function", None), "name", "") or "",
                input=_coerce_tool_args(getattr(getattr(tc, "function", None), "arguments", None)),
                id=getattr(tc, "id", "") or "",
            ))

        text = getattr(msg, "content", None) or ""
        if not calls and text:
            calls = _parse_text_tool_calls(text)
            if calls:
                logging.debug(
                    "_chat_openai: extracted %d tool call(s) from text fallback", len(calls)
                )

        usage = _coerce_usage(getattr(resp, "usage", None))
        usage["model"] = self.model
        return LLMResponse(
            tool_calls=calls,
            text=text,
            usage=usage,
            stop_reason=str(getattr(resp.choices[0], "finish_reason", "") or ""),
        )


async def fetch_available_models(provider: str, api_key: str) -> list[str]:
    """Query the provider's models list. Returns [DEFAULT_MODELS[provider]] on any error."""
    fallback = [DEFAULT_MODELS.get(provider, "gpt-4o")]
    try:
        if provider == "anthropic":
            import anthropic as _ant
            client = _ant.AsyncAnthropic(api_key=api_key)
            resp = await asyncio.wait_for(client.models.list(), timeout=8.0)
            return sorted(m.id for m in resp.data) or fallback

        if provider == "ollama":
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get("http://localhost:11434/api/tags")
                data = r.json()
            return [m["name"] for m in data.get("models", [])] or fallback

        import openai as _oai
        base_url = _OPENAI_BASE_URLS.get(provider)
        client = _oai.AsyncOpenAI(api_key=api_key or "nokey", base_url=base_url)
        resp = await asyncio.wait_for(client.models.list(), timeout=8.0)
        names = sorted(m.id for m in resp.data)
        if provider == "groq":
            text_keys = ("llama", "mixtral", "gemma", "whisper", "deepseek", "qwen")
            names = [n for n in names if any(k in n for k in text_keys)]
        elif provider == "gemini":
            gemini_text_keys = ("gemini",)
            gemini_exclude = ("embedding", "aqa", "imagen", "vision")
            names = [
                n for n in names
                if any(k in n for k in gemini_text_keys)
                and not any(x in n for x in gemini_exclude)
            ]
        return names or fallback
    except Exception:
        return fallback
