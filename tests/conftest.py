"""Shared pytest fixtures for the test suite.

Fixtures exported here are usable from any test file under tests/ without
explicit import. Conventions used across the suite:

* `sample_session`         - SessionManager pre-populated with cookies + bearer token.
* `sample_capture`         - CapturedResponse with a typical JSON payload.
* `no_color_env`           - Removes NO_COLOR and forces TERM=dumb.
* `forced_color_env`       - Removes NO_COLOR and sets TERM=xterm-256color.
* `make_text_response`     - Build a fake Anthropic text response (SimpleNamespace, legacy).
* `make_tool_use_response` - Build a fake Anthropic tool_use response (SimpleNamespace, legacy).
* `make_empty_response`    - Build a fake Anthropic response with no content blocks (legacy).
* `make_llm_text_response` - Build an LLMResponse with only text.
* `make_llm_tool_response` - Build an LLMResponse with a single tool call.
* `make_llm_empty_response`- Build an LLMResponse with no tool calls and no text.
* `make_captured_response` - Build a CapturedResponse with arbitrary fields.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from network.intercept.sniffer import CapturedResponse
from network.session.manager import SessionManager
from ai.provider import LLMResponse, ToolCall


_TEST_USAGE = {"input": 10, "output": 10, "model": "test"}


# ---------------------------------------------------------------------------
# Legacy helpers (kept for any tests that still use Anthropic SimpleNamespace
# response format in non-AI modules)
# ---------------------------------------------------------------------------

def make_text_response(text: str) -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def make_tool_use_response(name: str, input: dict) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=name, input=input)
    return SimpleNamespace(content=[block])


def make_empty_response() -> SimpleNamespace:
    return SimpleNamespace(content=[])


# ---------------------------------------------------------------------------
# LLMResponse helpers — used by AI agent tests
# ---------------------------------------------------------------------------

def make_llm_text_response(text: str) -> LLMResponse:
    return LLMResponse(tool_calls=[], text=text, usage=_TEST_USAGE)


def make_llm_tool_response(name: str, input: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(name=name, input=input)], text="", usage=_TEST_USAGE)


def make_llm_empty_response() -> LLMResponse:
    return LLMResponse(tool_calls=[], text="", usage={**_TEST_USAGE, "input": 0, "output": 0})


def mock_llm_client(response: LLMResponse | None = None, side_effect=None) -> MagicMock:
    """Return a MagicMock LLMClient whose chat() is an AsyncMock."""
    client = MagicMock()
    if side_effect is not None:
        client.chat = AsyncMock(side_effect=side_effect)
    else:
        client.chat = AsyncMock(return_value=response or make_llm_text_response(""))
    return client


def make_captured_response(
    url: str = "https://api.example.com/posts/1",
    status: int = 200,
    body: dict | list | None = None,
    headers: dict | None = None,
) -> CapturedResponse:
    body_bytes = json.dumps(body).encode() if body is not None else b""
    return CapturedResponse(
        url=url,
        status=status,
        headers=headers or {"content-type": "application/json"},
        body=body_bytes,
        json=body,
    )


@pytest.fixture
def sample_session() -> SessionManager:
    sm = SessionManager()
    sm.credentials.cookies = {"session": "abc123"}
    sm.credentials.bearer_token = "test-token-xyz"
    sm.credentials.csrf_token = "csrf-abc"
    return sm


@pytest.fixture
def sample_capture() -> CapturedResponse:
    return make_captured_response(
        body={
            "id": 1,
            "title": "Hello",
            "body": "World",
            "userId": 1,
            "tracking_id": "should-be-stripped",
            "metadata": {"nested": "should-be-stripped-too"},
        }
    )


@pytest.fixture
def no_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    yield


@pytest.fixture
def forced_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    yield
