from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai.discovery.planner import ToolSchema
from ai.provider import LLMClient


# ---- LLMClient.__init__ import-error edge cases ----



def test_init_anthropic_raises_import_error_when_package_missing(monkeypatch):
    """If `import anthropic` raises ImportError, LLMClient re-raises with hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="uv add anthropic"):
        LLMClient("anthropic", "sk-test")


def test_init_openai_raises_import_error_when_package_missing(monkeypatch):
    """If `import openai` raises ImportError, LLMClient re-raises with hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("No module named 'openai'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="uv add openai"):
        LLMClient("openai", "sk-test")


_TEST_TOOL = ToolSchema(
    name="route_to_domain",
    description="route",
    parameters={"type": "object", "properties": {"x": {"type": "string"}}},
)


# ---- LLMClient.__init__ ----

def test_init_anthropic_creates_async_anthropic_backend():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("anthropic", "sk-test")
    assert client.provider == "anthropic"
    assert client.model == "claude-sonnet-4-6"
    mock_cls.assert_called_once_with(api_key="sk-test")


def test_init_openai_uses_default_base_url():
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("openai", "sk-test")
    assert client.provider == "openai"
    assert client.model == "gpt-4o"
    mock_cls.assert_called_once_with(api_key="sk-test", base_url=None)


def test_init_groq_uses_groq_base_url():
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("groq", "gsk-test")
    assert client.provider == "groq"
    mock_cls.assert_called_once_with(
        api_key="gsk-test", base_url="https://api.groq.com/openai/v1"
    )


def test_init_ollama_uses_localhost_base_url_and_dummy_key():
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("ollama", "")
    assert client.provider == "ollama"
    assert client.model == "llama3.2"
    mock_cls.assert_called_once_with(
        api_key="ollama", base_url="http://localhost:11434/v1"
    )


def test_init_unknown_provider_raises():
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        with pytest.raises(ValueError, match="Unknown provider 'fake-provider'"):
            LLMClient("fake-provider", "key")


def test_init_case_insensitive_provider():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("ANTHROPIC", "sk-test")
    assert client.provider == "anthropic"


def test_init_uses_explicit_model_when_provided():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        client = LLMClient("anthropic", "sk-test", model="claude-opus-4-1")
    assert client.model == "claude-opus-4-1"


# ---- LLMClient.chat dispatch ----

async def test_chat_dispatches_to_anthropic_backend():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=5, output_tokens=7),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "sk-test")
        result = await client.chat(
            system="you are a router",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert result.usage == {"input": 5, "output": 7, "model": "claude-sonnet-4-6"}


async def test_chat_dispatches_to_openai_backend():
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="you are a router",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert result.text == "ok"
    assert result.usage["input"] == 3
    assert result.usage["output"] == 4


# ---- _chat_anthropic ----

async def test_chat_anthropic_extracts_text_and_tool_use():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="I'll route it."),
                    SimpleNamespace(type="tool_use", name="route_to_domain", input={"x": 1}),
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "sk-test")
        result = await client.chat(
            system="you are a router",
            messages=[{"role": "user", "content": "hi"}],
            tools=[_TEST_TOOL],
            max_tokens=64,
        )
    assert result.text == "I'll route it."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "route_to_domain"
    assert result.tool_calls[0].input == {"x": 1}
    call_kwargs = backend.messages.create.await_args.kwargs
    assert "tools" in call_kwargs
    assert call_kwargs["tools"] == [_TEST_TOOL.to_anthropic()]


async def test_chat_anthropic_omits_tools_when_empty():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "sk-test")
        await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert "tools" not in backend.messages.create.await_args.kwargs


# ---- _chat_openai ----

async def test_chat_openai_extracts_tool_calls_with_arguments():
    with patch("openai.AsyncOpenAI") as mock_cls:
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="route_to_domain", arguments='{"x": 1}')
        )
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[tool_call],
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[_TEST_TOOL],
            max_tokens=64,
        )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "route_to_domain"
    assert result.tool_calls[0].input == {"x": 1}
    call_kwargs = backend.chat.completions.create.await_args.kwargs
    assert call_kwargs["tools"] == [_TEST_TOOL.to_openai()]
    assert call_kwargs["tool_choice"] == "auto"
    assert call_kwargs["messages"][0] == {"role": "system", "content": "sys"}


async def test_chat_openai_handles_malformed_arguments():
    with patch("openai.AsyncOpenAI") as mock_cls:
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="x", arguments="not json")
        )
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=[tool_call])
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert result.text == "ok"
    assert result.tool_calls[0].input == {}


async def test_chat_openai_handles_null_usage():
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
                usage=None,
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert result.usage["input"] == 0
    assert result.usage["output"] == 0


async def test_chat_openai_omits_tools_when_empty():
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    kwargs = backend.chat.completions.create.await_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


# ── Timeout handling ────────────────────────────────────────────────────────

async def test_chat_anthropic_raises_runtime_error_on_timeout():
    """asyncio.TimeoutError inside _chat_anthropic is re-raised as RuntimeError."""
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")
        # Patch wait_for inside the provider module so TimeoutError propagates
        with patch("ai.provider.asyncio.wait_for", side_effect=TimeoutError()):
            with pytest.raises(RuntimeError, match="timed out"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_openai_raises_runtime_error_on_timeout():
    """asyncio.TimeoutError inside _chat_openai is re-raised as RuntimeError."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        with patch("ai.provider.asyncio.wait_for", side_effect=TimeoutError()):
            with pytest.raises(RuntimeError, match="timed out"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_anthropic_wraps_connect_error():
    """H13: anthropic.APIConnectionError is wrapped as RuntimeError."""
    import anthropic as _anthropic

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(
            side_effect=_anthropic.APIConnectionError(request=MagicMock())
        )
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(RuntimeError, match="No connection"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_anthropic_wraps_httpx_connect_error():
    """H13: httpx.ConnectError is wrapped as RuntimeError for anthropic backend."""
    import httpx

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(
            side_effect=httpx.ConnectError("refused")
        )
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(RuntimeError, match="No connection"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_anthropic_does_not_wrap_arbitrary_connect_named_error():
    """H13: arbitrary exception with 'connect' in name is NOT swallowed (type-based only)."""
    class FakeConnectError(Exception):
        pass

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(side_effect=FakeConnectError("refused"))
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            # Should propagate as FakeConnectError, NOT RuntimeError
            with pytest.raises(FakeConnectError):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_openai_reraises_unknown_exception():
    """Unknown exceptions (not connect/timeout) propagate as-is."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(side_effect=ValueError("unexpected"))
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(ValueError, match="unexpected"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_anthropic_reraises_non_connect_error():
    """Anthropic non-connectivity errors propagate as-is (not wrapped)."""
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(side_effect=KeyError("missing"))
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(KeyError, match="missing"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_anthropic_wraps_httpx_network_error():
    """H13: httpx.NetworkError is wrapped as RuntimeError for anthropic backend."""
    import httpx

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        backend = MagicMock()
        backend.messages.create = AsyncMock(side_effect=httpx.NetworkError("dns"))
        mock_cls.return_value = backend
        client = LLMClient("anthropic", "key")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(RuntimeError, match="No connection"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_openai_wraps_connect_error():
    """H13: openai.APIConnectionError is wrapped as RuntimeError."""
    import openai as _openai

    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            side_effect=_openai.APIConnectionError(request=MagicMock())
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(RuntimeError, match="No connection"):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


async def test_chat_openai_does_not_wrap_arbitrary_connect_named_error():
    """H13: arbitrary exception with 'connect' in name is NOT swallowed by OpenAI path."""
    class FakeConnectError(Exception):
        pass

    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            side_effect=FakeConnectError("refused")
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")

        async def fake_wait_for(coro, timeout):
            return await coro

        with patch("ai.provider.asyncio.wait_for", side_effect=fake_wait_for):
            # Should propagate as FakeConnectError, NOT RuntimeError
            with pytest.raises(FakeConnectError):
                await client.chat(
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    max_tokens=64,
                )


# ── Empty choices guard ─────────────────────────────────────────────────────

async def test_chat_openai_empty_choices_returns_empty_llm_response():
    """When API returns zero choices, return empty LLMResponse instead of crashing."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            max_tokens=64,
        )
    assert result.tool_calls == []
    assert result.text == ""
    assert result.usage["input"] == 0
    assert result.usage["output"] == 0


# ── fetch_available_models ──────────────────────────────────────────────────

async def test_fetch_available_models_anthropic_returns_sorted_model_ids():
    """Anthropic provider returns sorted model IDs from API."""
    from ai.provider import fetch_available_models

    fake_model_b = SimpleNamespace(id="claude-sonnet-4-6")
    fake_model_a = SimpleNamespace(id="claude-haiku-4-5")
    fake_resp = SimpleNamespace(data=[fake_model_b, fake_model_a])

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(return_value=fake_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        result = await fetch_available_models("anthropic", "sk-test")

    assert result == sorted(["claude-sonnet-4-6", "claude-haiku-4-5"])


async def test_fetch_available_models_ollama_parses_tags_response():
    """Ollama provider parses /api/tags JSON into model names."""
    import respx
    import httpx
    from ai.provider import fetch_available_models

    with respx.mock:
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"name": "llama3.2"}, {"name": "mistral"}]},
            )
        )
        result = await fetch_available_models("ollama", "")

    assert result == ["llama3.2", "mistral"]


async def test_fetch_available_models_openai_returns_sorted_ids():
    """OpenAI-compatible providers return sorted model IDs."""
    from ai.provider import fetch_available_models

    fake_models = [SimpleNamespace(id="gpt-4o-mini"), SimpleNamespace(id="gpt-4o")]
    fake_resp = SimpleNamespace(data=fake_models)

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await fetch_available_models("openai", "sk-test")

    assert result == ["gpt-4o", "gpt-4o-mini"]


async def test_fetch_available_models_groq_filters_to_known_models():
    """Groq provider filters out non-chat models."""
    from ai.provider import fetch_available_models

    fake_models = [
        SimpleNamespace(id="llama-3.3-70b"),
        SimpleNamespace(id="whisper-large-v3"),
        SimpleNamespace(id="some-image-model"),
    ]
    fake_resp = SimpleNamespace(data=fake_models)

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await fetch_available_models("groq", "gsk-test")

    assert "some-image-model" not in result
    assert "llama-3.3-70b" in result


async def test_fetch_available_models_returns_fallback_on_any_exception():
    """Any exception during model fetch returns the default model list (never raises)."""
    from ai.provider import fetch_available_models, DEFAULT_MODELS

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(side_effect=Exception("network error"))

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        result = await fetch_available_models("anthropic", "sk-test")

    assert result == [DEFAULT_MODELS["anthropic"]]


async def test_fetch_available_models_returns_fallback_on_timeout():
    """asyncio.TimeoutError during model fetch returns fallback list silently."""
    from ai.provider import fetch_available_models, DEFAULT_MODELS

    with patch("ai.provider.asyncio.wait_for", side_effect=TimeoutError()):
        result = await fetch_available_models("anthropic", "sk-test")

    assert result == [DEFAULT_MODELS["anthropic"]]


# ── ToolCall.id extraction in _chat_openai ──────────────────────────────────

async def test_chat_openai_extracts_tool_call_id():
    """_chat_openai must populate ToolCall.id from tc.id (not leave it as empty string)."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        tool_call = SimpleNamespace(
            id="call_abc123",
            function=SimpleNamespace(name="route_to_domain", arguments='{"x": 1}'),
        )
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[tool_call],
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[_TEST_TOOL],
            max_tokens=64,
        )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_abc123"


async def test_chat_openai_tool_call_id_defaults_to_empty_string_when_absent():
    """When tc.id attribute is missing (e.g. mock), ToolCall.id falls back to ''."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        # SimpleNamespace without id attribute
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="route_to_domain", arguments='{"x": 1}'),
        )
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[tool_call],
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[_TEST_TOOL],
            max_tokens=64,
        )
    assert result.tool_calls[0].id == ""


async def test_chat_openai_tool_call_id_none_coerced_to_empty_string():
    """When tc.id is None (some providers omit it), ToolCall.id must be '' not None."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        tool_call = SimpleNamespace(
            id=None,
            function=SimpleNamespace(name="route_to_domain", arguments="{}"),
        )
        backend = MagicMock()
        backend.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[tool_call],
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )
        )
        mock_cls.return_value = backend
        client = LLMClient("openai", "sk-test")
        result = await client.chat(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[_TEST_TOOL],
            max_tokens=64,
        )
    assert result.tool_calls[0].id == ""


# ── fetch_available_models — Gemini filtering ───────────────────────────────

async def test_fetch_available_models_gemini_filters_non_chat_models():
    """Gemini provider keeps only 'gemini-*' models and excludes embedding/aqa/imagen/vision."""
    from ai.provider import fetch_available_models

    fake_models = [
        SimpleNamespace(id="gemini-2.0-flash"),
        SimpleNamespace(id="gemini-1.5-pro"),
        SimpleNamespace(id="text-embedding-004"),
        SimpleNamespace(id="gemini-embedding-exp"),
        SimpleNamespace(id="aqa"),
        SimpleNamespace(id="imagen-3.0-generate-001"),
        SimpleNamespace(id="gemini-pro-vision"),
    ]
    fake_resp = SimpleNamespace(data=fake_models)

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await fetch_available_models("gemini", "api-key")

    # Chat-capable models are kept
    assert "gemini-2.0-flash" in result
    assert "gemini-1.5-pro" in result
    # Non-chat models are excluded
    assert "text-embedding-004" not in result
    assert "gemini-embedding-exp" not in result
    assert "aqa" not in result
    assert "imagen-3.0-generate-001" not in result
    assert "gemini-pro-vision" not in result


async def test_fetch_available_models_gemini_returns_fallback_when_all_filtered():
    """When all Gemini models are filtered out, the fallback default is returned."""
    from ai.provider import fetch_available_models, DEFAULT_MODELS

    fake_models = [
        SimpleNamespace(id="text-embedding-004"),
        SimpleNamespace(id="imagen-3.0-generate-001"),
    ]
    fake_resp = SimpleNamespace(data=fake_models)

    mock_client = MagicMock()
    mock_client.models.list = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await fetch_available_models("gemini", "api-key")

    assert result == [DEFAULT_MODELS["gemini"]]
