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
from dataclasses import dataclass
from typing import Any

import httpx


# Default primary model per provider
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-4o",
    "groq":      "llama-3.3-70b-versatile",
    "gemini":    "gemini-2.0-flash",
    "together":  "meta-llama/Llama-3-70b-chat-hf",
    "ollama":    "llama3.2",
}

# Default cheap model for the recovery hot-path
DEFAULT_RECOVERY_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "groq":      "llama-3.1-8b-instant",
    "gemini":    "gemini-2.0-flash",
    "together":  "meta-llama/Llama-3-8b-chat-hf",
    "ollama":    "llama3.2",
}

# Base URLs for OpenAI-compatible providers (None = OpenAI default)
_OPENAI_BASE_URLS: dict[str, str | None] = {
    "openai":   None,
    "groq":     "https://api.groq.com/openai/v1",
    "gemini":   "https://generativelanguage.googleapis.com/v1beta/openai/",
    "together": "https://api.together.xyz/v1",
    "ollama":   "http://localhost:11434/v1",
}

# Env var to check per provider when --api-key is not supplied
PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "groq":      "GROQ_API_KEY",
    "gemini":    "GEMINI_API_KEY",
    "together":  "TOGETHER_API_KEY",
    "ollama":    "",  # local — no key required
}


@dataclass
class ToolCall:
    name: str
    input: dict
    id: str = ""  # tool_use block ID for Anthropic conversation replay


@dataclass
class LLMResponse:
    tool_calls: list[ToolCall]
    text: str
    usage: dict  # {"input": int, "output": int, "model": str}


class LLMClient:
    """Unified async LLM client. One instance per provider + model combination.

    `tools` passed to `chat()` must be `ToolSchema` objects from planner.py —
    each provider renders them in its own format internally.
    """

    def __init__(self, provider: str, api_key: str, model: str | None = None) -> None:
        self.provider = provider.lower()
        self.model    = model or DEFAULT_MODELS.get(self.provider, "gpt-4o")

        if self.provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                raise ImportError("Install anthropic: uv add anthropic")
            self._backend = anthropic.AsyncAnthropic(api_key=api_key)
        elif self.provider in _OPENAI_BASE_URLS:
            try:
                import openai
            except ImportError:
                raise ImportError("Install openai: uv add openai")
            self._backend = openai.AsyncOpenAI(
                api_key=api_key or "ollama",   # ollama ignores the key
                base_url=_OPENAI_BASE_URLS[self.provider],
            )
        else:
            raise ValueError(
                f"Unknown provider {self.provider!r}. "
                f"Choose from: {', '.join(DEFAULT_MODELS)}"
            )

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[Any],   # list[ToolSchema]; pass [] for text-only calls
        max_tokens: int,
    ) -> LLMResponse:
        if self.provider == "anthropic":
            return await self._chat_anthropic(system, messages, tools, max_tokens)
        return await self._chat_openai(system, messages, tools, max_tokens)

    # ------------------------------------------------------------------
    # Provider backends
    # ------------------------------------------------------------------

    async def _chat_anthropic(
        self, system: str, messages: list[dict], tools: list[Any], max_tokens: int
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
                self._backend.messages.create(**kwargs), timeout=60.0
            )
        except TimeoutError:
            raise RuntimeError(f"LLM API timed out after 60s ({self.provider}/{self.model}).")
        except Exception as exc:
            import anthropic as _anthropic
            if isinstance(exc, (_anthropic.APIConnectionError, httpx.ConnectError,
                                 httpx.NetworkError)):
                raise RuntimeError(f"No connection to {self.provider} API: {exc}") from exc
            raise

        calls, text = [], ""
        for block in resp.content:
            if block.type == "tool_use":
                calls.append(ToolCall(
                    name=block.name, input=block.input, id=getattr(block, "id", "")
                ))
            elif block.type == "text":
                text = block.text

        return LLMResponse(
            tool_calls=calls,
            text=text,
            usage={
                "input":  resp.usage.input_tokens,
                "output": resp.usage.output_tokens,
                "model":  self.model,
            },
        )

    async def _chat_openai(
        self, system: str, messages: list[dict], tools: list[Any], max_tokens: int
    ) -> LLMResponse:
        oai_messages = [{"role": "system", "content": system}] + messages
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
                self._backend.chat.completions.create(**kwargs), timeout=60.0
            )
        except TimeoutError:
            raise RuntimeError(f"LLM API timed out after 60s ({self.provider}/{self.model}).")
        except Exception as exc:
            import openai as _openai
            if isinstance(exc, (_openai.APIConnectionError, httpx.ConnectError,
                                 httpx.NetworkError)):
                raise RuntimeError(f"No connection to {self.provider} API: {exc}") from exc
            raise

        if not resp.choices:
            return LLMResponse(
                tool_calls=[], text="",
                usage={"input": 0, "output": 0, "model": self.model},
            )

        msg = resp.choices[0].message

        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, AttributeError):
                args = {}
            calls.append(ToolCall(
                name=tc.function.name,
                input=args,
                id=getattr(tc, "id", "") or "",
            ))

        u = resp.usage
        return LLMResponse(
            tool_calls=calls,
            text=msg.content or "",
            usage={
                "input":  u.prompt_tokens      if u else 0,
                "output": u.completion_tokens   if u else 0,
                "model":  self.model,
            },
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

        # OpenAI-compatible providers
        import openai as _oai
        base_url = _OPENAI_BASE_URLS.get(provider)
        client = _oai.AsyncOpenAI(api_key=api_key or "nokey", base_url=base_url)
        resp = await asyncio.wait_for(client.models.list(), timeout=8.0)
        names = sorted(m.id for m in resp.data)
        if provider == "groq":
            # Groq returns audio/image models too — keep only text/chat models
            text_keys = ("llama", "mixtral", "gemma", "whisper", "deepseek", "qwen")
            names = [n for n in names if any(k in n for k in text_keys)]
        elif provider == "gemini":
            # Gemini returns embedding/image/audio/AQA models — keep only generative text models
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
