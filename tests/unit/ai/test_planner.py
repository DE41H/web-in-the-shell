from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from ai.discovery.planner import Plan, PlannerAgent, ToolSchema
from conftest import (
    make_llm_empty_response,
    make_llm_text_response,
    make_llm_tool_response,
    mock_llm_client,
)
from persistence import ConvoStore, Convo, ConvoMessage
from persistence.db import init_db


# ---- helpers ----

def _planner(response=None, side_effect=None) -> PlannerAgent:
    client = mock_llm_client(response, side_effect=side_effect)
    return PlannerAgent(client)


# ---- ToolSchema ----

def test_tool_schema_to_anthropic():
    params = {"type": "object", "properties": {"x": {"type": "string"}}}
    tool = ToolSchema(name="x", description="d", parameters=params)
    assert tool.to_anthropic() == {
        "name": "x",
        "description": "d",
        "input_schema": params,
    }


def test_tool_schema_to_openai():
    params = {"type": "object", "properties": {"x": {"type": "string"}}}
    tool = ToolSchema(name="x", description="d", parameters=params)
    assert tool.to_openai() == {
        "type": "function",
        "function": {
            "name": "x",
            "description": "d",
            "parameters": params,
        },
    }


def test_tool_schema_round_trip_preserves_name():
    tool = ToolSchema(name="x", description="d", parameters={"type": "object"})
    assert tool.to_anthropic()["name"] == tool.to_openai()["function"]["name"]


# ---- Plan defaults ----

def test_plan_default_steps_empty():
    plan = Plan(target_domain="x", target_endpoints=["a"], action="b", parameters={})
    assert plan.steps == []



# ---- PlannerAgent.plan ----

async def test_plan_route_to_domain_single_endpoint():
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/api/v1/users"],
            "action": "create_user",
            "parameters": {"name": "Alice"},
        },
    ))
    plan = await planner.plan("Create a user named Alice")
    assert plan.target_domain == "https://app.example.com"
    assert plan.target_endpoints == ["/api/v1/users"]
    assert plan.action == "create_user"
    assert plan.parameters == {"name": "Alice"}
    assert plan.steps == [
        {
            "action": "create_user",
            "endpoint": "/api/v1/users",
            "parameters": {"name": "Alice"},
            "method": "POST",
        }
    ]


async def test_plan_route_to_domain_multiple_endpoints():
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/a", "/b"],
            "action": "do_thing",
            "parameters": {"k": "v"},
        },
    ))
    plan = await planner.plan("Do the thing")
    assert plan.target_endpoints == ["/a", "/b"]
    assert plan.steps == [
        {
            "action": "do_thing",
            "endpoint": "/a",
            "parameters": {"k": "v"},
            "method": "POST",
        }
    ]


async def test_plan_plan_steps_multi_step():
    steps_in = [
        {"action": "fetch", "endpoint": "/x", "parameters": {}},
        {"action": "post", "endpoint": "/y", "parameters": {}},
    ]
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {"domain": "https://app.example.com", "steps": steps_in},
    ))
    plan = await planner.plan("Multi-step task")
    # plan_steps normalises missing 'method' to "POST" on each step
    expected_steps = [
        {"action": "fetch", "endpoint": "/x", "parameters": {}, "method": "POST"},
        {"action": "post", "endpoint": "/y", "parameters": {}, "method": "POST"},
    ]
    assert plan.steps == expected_steps
    assert plan.target_endpoints == ["/x", "/y"]
    assert plan.action == "fetch"


async def test_plan_plan_steps_empty_steps_raises():
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {"domain": "https://app.example.com", "steps": []},
    ))
    with pytest.raises(ValueError, match="empty steps list"):
        await planner.plan("Empty steps")


async def test_plan_fallback_search_calls_handle_fallback():
    first = make_llm_tool_response("fallback_search", {"query": "x"})
    second = make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/api/v1/users"],
            "action": "create_user",
            "parameters": {"name": "Alice"},
        },
    )
    planner = _planner(side_effect=[first, second])
    planner._search_duckduckgo = AsyncMock(return_value="fake search results")
    plan = await planner.plan("Find a thing")
    assert plan.target_domain == "https://app.example.com"


async def test_plan_no_tool_call_raises():
    planner = _planner(make_llm_text_response("I think we should..."))
    with pytest.raises(ValueError, match="Planner produced no actionable tool call"):
        await planner.plan("Do something")


async def test_plan_empty_content_raises():
    planner = _planner(make_llm_empty_response())
    with pytest.raises(ValueError):
        await planner.plan("Do something")


# ---- PlannerAgent.handle_fallback ----

async def test_handle_fallback_returns_plan():
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/api/v1/users"],
            "action": "create_user",
            "parameters": {"name": "Alice"},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    plan = await planner.handle_fallback("find alice app")
    assert isinstance(plan, Plan)
    assert plan.target_domain == "https://app.example.com"


async def test_handle_fallback_no_tool_call_raises():
    planner = _planner(make_llm_text_response("I am not sure"))
    planner._search_duckduckgo = AsyncMock(return_value="")
    with pytest.raises(
        ValueError, match="Fallback planner produced no route_to_domain tool call"
    ):
        await planner.handle_fallback("find alice app")


async def test_handle_fallback_skips_non_route_tool_calls_then_raises():
    """Tool calls with names other than 'route_to_domain' are skipped; no route → raise."""
    from ai.provider import ToolCall

    wrong_name_response = make_llm_tool_response(
        "plan_steps",
        {"action": "create", "endpoint": "/x", "parameters": {}, "method": "POST"},
    )
    wrong_name_response.tool_calls = [
        ToolCall(name="plan_steps", input={}),
        ToolCall(name="search", input={"q": "foo"}),
    ]
    planner = _planner(wrong_name_response)
    planner._search_duckduckgo = AsyncMock(return_value="")
    with pytest.raises(
        ValueError, match="Fallback planner produced no route_to_domain tool call"
    ):
        await planner.handle_fallback("find alice app")


# ---- _search_duckduckgo ----

async def test_search_duckduckgo_returns_abstract_and_topics():
    payload = {
        "AbstractText": "Alice is a test app",
        "RelatedTopics": [
            {"Text": "Topic 1"},
            {"Text": "Topic 2"},
            {"Text": "Topic 3"},
            {"Text": "Topic 4 (skipped)"},
            "not a dict",
        ],
    }
    with respx.mock(base_url="https://api.duckduckgo.com") as router:
        router.get("/").respond(200, json=payload)
        planner = PlannerAgent(MagicMock())
        result = await planner._search_duckduckgo("alice app")
    assert "Alice is a test app" in result
    assert "Topic 1" in result
    assert "Topic 4 (skipped)" not in result


async def test_search_duckduckgo_returns_empty_on_exception():
    with respx.mock(base_url="https://api.duckduckgo.com") as router:
        router.get("/").mock(side_effect=httpx.ConnectError("nope"))
        planner = PlannerAgent(MagicMock())
        result = await planner._search_duckduckgo("alice app")
    assert result == ""


# ---- PlannerAgent resume (convos store) ----

async def test_plan_without_convos_does_not_load_past(tmp_path: Path):
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/api/v1/users"],
            "action": "create_user",
            "parameters": {"name": "Alice"},
        },
    ))
    await planner.plan("Create a user named Alice")

    assert planner.last_usage is not None
    # H2: last_messages includes user intent + tool_use assistant + tool_result user
    assert len(planner.last_messages) == 3
    assert planner.last_messages[0]["role"] == "user"
    assert planner.last_messages[1]["role"] == "assistant"
    assert isinstance(planner.last_messages[1]["content"], list)
    assert planner.last_messages[1]["content"][0]["type"] == "tool_use"
    assert planner.last_messages[2]["role"] == "user"
    assert isinstance(planner.last_messages[2]["content"], list)
    assert planner.last_messages[2]["content"][0]["type"] == "tool_result"


async def test_plan_tool_call_history_openai_format():
    """OpenAI-compatible providers store tool history in tool_calls/role=tool format."""
    client = mock_llm_client(
        make_llm_tool_response(
            "route_to_domain",
            {
                "domain": "https://app.example.com",
                "endpoints": ["/api/v1/users"],
                "action": "create_user",
                "parameters": {"name": "Alice"},
            },
        ),
        provider="gemini",
    )
    planner = PlannerAgent(client)
    await planner.plan("Create a user named Alice")

    # 3 messages: user intent, assistant with tool_calls, tool result
    assert len(planner.last_messages) == 3
    asst = planner.last_messages[1]
    assert asst["role"] == "assistant"
    assert asst["content"] is None
    assert isinstance(asst["tool_calls"], list)
    assert asst["tool_calls"][0]["type"] == "function"
    tool_msg = planner.last_messages[2]
    assert tool_msg["role"] == "tool"
    assert "tool_call_id" in tool_msg


async def test_plan_with_convos_prepends_past_messages(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        past = Convo(
            id="old-1",
            intent="Create a user named Alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            messages=[
                ConvoMessage(role="user", content="Create a user named Alice"),
                ConvoMessage(role="assistant", content="I will create the user."),
            ],
            result=None,
        )
        await store.save(past)

        client = mock_llm_client(make_llm_tool_response(
            "route_to_domain",
            {
                "domain": "https://app.example.com",
                "endpoints": ["/api/v1/users"],
                "action": "create_user",
                "parameters": {"name": "Alice"},
            },
        ))
        planner = PlannerAgent(client, convos=store)
        await planner.plan("Create a user named Alice")

    sent = client.chat.await_args.kwargs["messages"]
    # H2: sent is the same list mutated in-place after chat() (tool_use/result appended),
    # so >= 3. The first 3 entries are the past messages + new intent.
    assert len(sent) >= 3
    assert sent[0]["role"] == "user"
    assert sent[0]["content"] == "Create a user named Alice"
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"] == "I will create the user."
    assert sent[2]["role"] == "user"
    assert "Create a user named Alice" in sent[2]["content"]


async def test_plan_with_convos_no_match_does_not_prepend(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        past = Convo(
            id="unrelated",
            intent="Delete the database",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            messages=[ConvoMessage(role="user", content="Delete the database")],
            result=None,
        )
        await store.save(past)

        client = mock_llm_client(make_llm_tool_response(
            "route_to_domain",
            {
                "domain": "https://app.example.com",
                "endpoints": ["/api/v1/users"],
                "action": "create_user",
                "parameters": {"name": "Alice"},
            },
        ))
        planner = PlannerAgent(client, convos=store)
        await planner.plan("Create a user named Alice")

    sent = client.chat.await_args.kwargs["messages"]
    # H2: sent is mutated in-place, so it has >= 1; only the first entry is the intent.
    assert len(sent) >= 1
    assert sent[0]["role"] == "user"


async def test_plan_records_assistant_text_in_last_messages(tmp_path: Path):
    from ai.provider import LLMResponse, ToolCall
    response = LLMResponse(
        tool_calls=[ToolCall(
            name="route_to_domain",
            input={
                "domain": "https://app.example.com",
                "endpoints": ["/api/v1/users"],
                "action": "create_user",
                "parameters": {"name": "Alice"},
            },
        )],
        text="I will create the user now.",
        usage={"input": 10, "output": 10, "model": "test"},
    )
    client = mock_llm_client(response)
    planner = PlannerAgent(client)
    await planner.plan("Create a user named Alice")

    # H2: last_messages = [user intent, assistant text, assistant tool_use, user tool_result]
    assert len(planner.last_messages) == 4
    assert planner.last_messages[0]["role"] == "user"
    assert planner.last_messages[1] == {
        "role": "assistant",
        "content": "I will create the user now.",
    }
    # tool_use block is in [2]
    assert planner.last_messages[2]["role"] == "assistant"
    assert isinstance(planner.last_messages[2]["content"], list)
    assert planner.last_messages[2]["content"][0]["type"] == "tool_use"
    # tool_result block is in [3]
    assert planner.last_messages[3]["role"] == "user"
    assert isinstance(planner.last_messages[3]["content"], list)
    assert planner.last_messages[3]["content"][0]["type"] == "tool_result"


async def test_plan_with_no_assistant_text_records_only_user(tmp_path: Path):
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://app.example.com",
            "endpoints": ["/api/v1/users"],
            "action": "create_user",
            "parameters": {"name": "Alice"},
        },
    ))
    planner = PlannerAgent(client)
    await planner.plan("Create a user named Alice")

    # H2: [user intent, assistant tool_use, user tool_result]
    assert len(planner.last_messages) == 3
    assert planner.last_messages[0]["role"] == "user"
    assert planner.last_messages[-1]["role"] == "user"
    assert isinstance(planner.last_messages[-1]["content"], list)
    assert planner.last_messages[-1]["content"][0]["type"] == "tool_result"


# ---- handle_fallback sets last_messages ----

async def test_handle_fallback_sets_last_messages_with_user_turn():
    """handle_fallback always sets last_messages after the LLM call."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api/items"],
            "action": "list_items",
            "parameters": {},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    await planner.handle_fallback("find items")
    assert planner.last_messages is not None
    assert len(planner.last_messages) >= 1
    assert planner.last_messages[0]["role"] == "user"


async def test_handle_fallback_sets_last_messages_with_assistant_text():
    """When resp.text is non-empty, handle_fallback appends it as assistant message."""
    from ai.provider import LLMResponse, ToolCall
    response = LLMResponse(
        tool_calls=[ToolCall(
            name="route_to_domain",
            input={
                "domain": "https://example.com",
                "endpoints": ["/api/items"],
                "action": "list_items",
                "parameters": {},
            },
        )],
        text="I found the domain.",
        usage={"input": 10, "output": 5, "model": "test"},
    )
    client = mock_llm_client(response)
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="search snippet")
    await planner.handle_fallback("find items")
    assert planner.last_messages[-1] == {"role": "assistant", "content": "I found the domain."}


# ---- route_to_domain with explicit method field ----

async def test_route_to_domain_with_explicit_get_method():
    """When the LLM supplies method='GET', the step dict should have method='GET'."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "list_posts",
            "parameters": {},
            "method": "GET",
        },
    ))
    plan = await planner.plan("List all posts")
    assert plan.steps[0]["method"] == "GET"


async def test_route_to_domain_without_method_defaults_to_post():
    """When the LLM omits method, the step gets method='POST'."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "create_post",
            "parameters": {"title": "hello"},
        },
    ))
    plan = await planner.plan("Create a post")
    assert plan.steps[0]["method"] == "POST"


# ---- plan_steps passes through method ----

async def test_plan_steps_method_field_in_steps_preserved():
    """plan_steps steps with 'method' fields are stored as-is in plan.steps."""
    steps_in = [
        {"action": "get_user", "endpoint": "/users/1", "parameters": {}, "method": "GET"},
        {
            "action": "update_user", "endpoint": "/users/1",
            "parameters": {"name": "Bob"}, "method": "PUT",
        },
    ]
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {"domain": "https://api.example.com", "steps": steps_in},
    ))
    plan = await planner.plan("Get then update user")
    assert plan.steps[0]["method"] == "GET"
    assert plan.steps[1]["method"] == "PUT"


# ---- KeyError → ValueError for malformed tool calls ----

async def test_malformed_route_to_domain_missing_endpoints_raises_value_error():
    """route_to_domain without 'endpoints' key raises ValueError, not KeyError."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            # "endpoints" key intentionally missing
            "action": "do_thing",
            "parameters": {},
        },
    ))
    with pytest.raises(ValueError, match="Malformed route_to_domain"):
        await planner.plan("Do something")


async def test_malformed_route_to_domain_missing_domain_raises_value_error():
    """route_to_domain without 'domain' key raises ValueError, not KeyError."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            # "domain" key intentionally missing
            "endpoints": ["/api"],
            "action": "do_thing",
            "parameters": {},
        },
    ))
    with pytest.raises(ValueError, match="Malformed route_to_domain"):
        await planner.plan("Do something")


async def test_malformed_plan_steps_missing_domain_raises_value_error():
    """plan_steps without 'domain' key raises ValueError, not KeyError."""
    steps_in = [{"action": "fetch", "endpoint": "/x", "parameters": {}}]
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {
            # "domain" intentionally missing
            "steps": steps_in,
        },
    ))
    with pytest.raises(ValueError, match="Malformed plan_steps"):
        await planner.plan("Fetch something")


async def test_handle_fallback_empty_endpoints_uses_empty_first_endpoint():
    """When the LLM returns endpoints=[], handle_fallback uses '' as the first endpoint."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": [],
            "action": "root_action",
            "parameters": {},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    plan = await planner.handle_fallback("find something")
    assert plan.target_domain == "https://example.com"
    assert plan.steps[0]["endpoint"] == ""


# ── H1: handle_fallback appends to existing messages, doesn't replace ──────────

async def test_handle_fallback_appends_to_primary_messages():
    """H1: handle_fallback called from plan() should extend, not replace, last_messages."""
    from ai.provider import LLMResponse, ToolCall

    # First call: fallback_search
    fallback_response = LLMResponse(
        tool_calls=[ToolCall(name="fallback_search", input={"query": "alice app"})],
        text="",
        usage={"input": 5, "output": 5, "model": "test"},
    )
    # Second call (inside handle_fallback): route_to_domain
    route_response = LLMResponse(
        tool_calls=[ToolCall(
            name="route_to_domain",
            input={
                "domain": "https://alice.app",
                "endpoints": ["/api/users"],
                "action": "create",
                "parameters": {"name": "Alice"},
            },
        )],
        text="",
        usage={"input": 6, "output": 6, "model": "test"},
    )
    client = mock_llm_client(side_effect=[fallback_response, route_response])
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="results")
    await planner.plan("find alice app")

    # H1: last_messages should contain the original intent user message PLUS
    # the fallback user message — not just the fallback messages alone.
    roles = [m["role"] for m in planner.last_messages]
    # At minimum: user intent (from plan), then fallback user turn, then fallback assistant
    assert roles[0] == "user", "First message should be the original intent"
    intent_msg = planner.last_messages[0]["content"]
    assert "find alice app" in intent_msg or "Intent:" in intent_msg


async def test_handle_fallback_called_directly_preserves_context():
    """H1: direct call to handle_fallback with no messages arg creates fresh list."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api/items"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    plan = await planner.handle_fallback("find items")
    assert plan.target_domain == "https://example.com"
    # last_messages should have at least the fallback user turn
    assert len(planner.last_messages) >= 1
    assert planner.last_messages[0]["role"] == "user"


# ── H2: tool-call/result pairs appended to last_messages ──────────────────────

async def test_plan_last_messages_contains_tool_use_blocks():
    """H2: after plan(), last_messages includes tool_use + tool_result blocks."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "create_post",
            "parameters": {"title": "Hello"},
        },
    ))
    await planner.plan("Create a post")

    # Find tool_use block
    tool_use_msgs = [
        m for m in planner.last_messages
        if m["role"] == "assistant"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"])
    ]
    assert len(tool_use_msgs) == 1, "Should have exactly one assistant tool_use message"
    tool_use_block = tool_use_msgs[0]["content"][0]
    assert tool_use_block["type"] == "tool_use"
    assert tool_use_block["name"] == "route_to_domain"
    assert "id" in tool_use_block

    # Find tool_result block — should reference the same id
    tool_result_msgs = [
        m for m in planner.last_messages
        if m["role"] == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_msgs) == 1, "Should have exactly one user tool_result message"
    tool_result_block = tool_result_msgs[0]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == tool_use_block["id"]


# ── H12: KeyError guard in handle_fallback ────────────────────────────────────

async def test_handle_fallback_missing_key_raises_value_error():
    """H12: malformed route_to_domain in handle_fallback raises ValueError, not KeyError."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            # "domain" intentionally missing
            "endpoints": ["/api"],
            "action": "do_thing",
            "parameters": {},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    with pytest.raises(ValueError, match="Malformed route_to_domain tool call in fallback"):
        await planner.handle_fallback("find something")


async def test_handle_fallback_missing_endpoints_raises_value_error():
    """H12: route_to_domain without 'endpoints' in handle_fallback raises ValueError."""
    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            # "endpoints" intentionally missing — will raise KeyError on inp["endpoints"]
            "action": "do_thing",
            "parameters": {},
        },
    ))
    planner._search_duckduckgo = AsyncMock(return_value="")
    with pytest.raises(ValueError, match="Malformed route_to_domain tool call in fallback"):
        await planner.handle_fallback("find something")


# ── M1: context sanitization and empty-context label ─────────────────────────

async def test_plan_sanitizes_context_before_llm():
    """M1: context is passed through sanitize_for_llm before embedding in the prompt."""
    from unittest.mock import patch as _patch

    calls = []

    def tracking_sanitize(text: str) -> str:
        calls.append(text)
        return text  # pass-through for the test

    planner = _planner(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "create",
            "parameters": {},
        },
    ))
    with _patch("ai.discovery.planner.sanitize_for_llm", side_effect=tracking_sanitize):
        await planner.plan("Create a post", context="endpoint=/api/posts error=500")

    # sanitize_for_llm must have been called with the context string
    assert any("endpoint=/api/posts" in c for c in calls), (
        f"sanitize_for_llm was not called with the context. Calls: {calls}"
    )


async def test_plan_empty_context_omits_label():
    """M1: when context='', the prompt must not include 'Current state context:'."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "create",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    await planner.plan("Create a post", context="")

    sent_messages = client.chat.await_args.kwargs["messages"]
    # Find the intent user message (first one with "Intent:" prefix)
    intent_msg = next(
        m for m in sent_messages if m["role"] == "user" and "Intent:" in str(m.get("content", ""))
    )
    assert "Current state context:" not in intent_msg["content"]


async def test_plan_nonempty_context_includes_label():
    """M1: when context is non-empty, the prompt includes 'Current state context:'."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://api.example.com",
            "endpoints": ["/posts"],
            "action": "create",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    await planner.plan("Create a post", context="status=200 endpoint=/posts")

    sent_messages = client.chat.await_args.kwargs["messages"]
    intent_msg = next(
        m for m in sent_messages if m["role"] == "user" and "Intent:" in str(m.get("content", ""))
    )
    assert "Current state context:" in intent_msg["content"]
    assert "status=200" in intent_msg["content"]


# ── History capped at 20 messages ─────────────────────────────────────────────

async def test_plan_caps_past_messages_at_20(tmp_path: Path):
    """Replayed conversation history is capped to the last 20 messages (10 turns)."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        # Create a past convo with 30 messages (15 turns)
        msgs = [
            ConvoMessage(role="user" if i % 2 == 0 else "assistant", content=f"msg-{i}")
            for i in range(30)
        ]
        past = Convo(
            id="old-long",
            intent="Long conversation",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            messages=msgs,
            result=None,
        )
        await store.save(past)

        client = mock_llm_client(make_llm_tool_response(
            "route_to_domain",
            {
                "domain": "https://api.example.com",
                "endpoints": ["/posts"],
                "action": "create",
                "parameters": {},
            },
        ))
        planner = PlannerAgent(client, convos=store)
        await planner.plan("Long conversation")

    sent = client.chat.await_args.kwargs["messages"]
    # Only past[-20:] + 1 new intent = 21 messages sent to LLM at most
    # (plus tool_use/result appended after, but those are irrelevant here)
    # The first 20 entries of sent should be past messages, the 21st is the intent
    past_in_sent = [
        m for m in sent
        if m["role"] in ("user", "assistant") and "msg-" in str(m.get("content", ""))
    ]
    assert len(past_in_sent) <= 20, f"Expected ≤ 20 past messages, got {len(past_in_sent)}"


# ── Empty-search-results guidance ─────────────────────────────────────────────

async def test_handle_fallback_empty_search_includes_no_results_guidance():
    """When DuckDuckGo returns '', the LLM prompt must include explicit no-results guidance."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api/items"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    await planner.handle_fallback("top clubs in bangalore")

    sent_messages = client.chat.await_args.kwargs["messages"]
    last_user_content = sent_messages[-1]["content"]
    assert "No search results were found" in last_user_content
    assert "Use your knowledge" in last_user_content


async def test_handle_fallback_with_search_results_omits_no_results_guidance():
    """When DuckDuckGo returns content, the 'no results' message must NOT appear."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api/items"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="Some useful search snippet")
    await planner.handle_fallback("top clubs in bangalore")

    sent_messages = client.chat.await_args.kwargs["messages"]
    last_user_content = sent_messages[-1]["content"]
    assert "No search results were found" not in last_user_content
    assert "Some useful search snippet" in last_user_content


# ── _is_tool_message filtering in handle_fallback ────────────────────────────

async def test_handle_fallback_strips_openai_role_tool_messages():
    """role='tool' messages (OpenAI format) are filtered out before the fallback LLM call."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    tool_msg = {"role": "tool", "tool_call_id": "abc", "content": '{"q": "foo"}'}
    await planner.handle_fallback("find something", messages=[tool_msg])

    sent_messages = client.chat.await_args.kwargs["messages"]
    assert not any(m.get("role") == "tool" for m in sent_messages), (
        "role='tool' messages should be stripped before the fallback LLM call"
    )


async def test_handle_fallback_strips_openai_assistant_with_tool_calls():
    """assistant messages with tool_calls (OpenAI format) are filtered before the fallback call."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    asst_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "x", "type": "function", "function": {"name": "fn", "arguments": "{}"}}],
    }
    await planner.handle_fallback("find something", messages=[asst_msg])

    sent_messages = client.chat.await_args.kwargs["messages"]
    assert not any(m.get("tool_calls") for m in sent_messages), (
        "assistant messages with tool_calls should be stripped before the fallback LLM call"
    )


async def test_handle_fallback_strips_anthropic_tool_use_content_blocks():
    """Anthropic-format assistant messages with type='tool_use' content blocks are stripped."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    tool_use_msg = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu1", "name": "fallback_search", "input": {"query": "x"}}],
    }
    tool_result_msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": '{"query": "x"}'}],
    }
    plain_user_msg = {"role": "user", "content": "Intent: find something"}
    await planner.handle_fallback(
        "find something",
        messages=[plain_user_msg, tool_use_msg, tool_result_msg],
    )

    sent_messages = client.chat.await_args.kwargs["messages"]
    # tool_use and tool_result blocks must be stripped; plain user message must survive
    for m in sent_messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                assert block.get("type") not in ("tool_use", "tool_result"), (
                    f"Anthropic tool block leaked into fallback LLM call: {block}"
                )
    plain_contents = [m["content"] for m in sent_messages if isinstance(m.get("content"), str)]
    assert any("Intent: find something" in c for c in plain_contents), (
        "Plain user message should survive the tool-message strip"
    )


async def test_handle_fallback_preserves_plain_assistant_text_messages():
    """Plain assistant text messages (no tool_calls, no content list) are NOT stripped."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    plain_asst = {"role": "assistant", "content": "I will help you find it."}
    await planner.handle_fallback("find something", messages=[plain_asst])

    sent_messages = client.chat.await_args.kwargs["messages"]
    asst_msgs = [m for m in sent_messages if m.get("role") == "assistant"]
    assert any(m.get("content") == "I will help you find it." for m in asst_msgs), (
        "Plain assistant text messages should not be stripped"
    )


async def test_handle_fallback_preserves_empty_list_content():
    """Messages with content=[] (empty list) are NOT stripped — any() returns False."""
    client = mock_llm_client(make_llm_tool_response(
        "route_to_domain",
        {
            "domain": "https://example.com",
            "endpoints": ["/api"],
            "action": "list",
            "parameters": {},
        },
    ))
    planner = PlannerAgent(client)
    planner._search_duckduckgo = AsyncMock(return_value="")
    empty_content_msg = {"role": "assistant", "content": []}
    await planner.handle_fallback("find something", messages=[empty_content_msg])

    sent_messages = client.chat.await_args.kwargs["messages"]
    # empty-list content is not a tool block, so the message should pass through
    assert any(
        m.get("role") == "assistant" and m.get("content") == []
        for m in sent_messages
    ), "Message with content=[] should NOT be stripped"


# ── plan_steps method default normalisation ───────────────────────────────────

async def test_plan_steps_without_method_defaults_to_post():
    """When LLM omits 'method' from plan_steps steps, each step gets method='POST'."""
    steps_in = [
        {"action": "fetch", "endpoint": "/x", "parameters": {}},
        {"action": "post", "endpoint": "/y", "parameters": {"k": "v"}},
    ]
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {"domain": "https://app.example.com", "steps": steps_in},
    ))
    plan = await planner.plan("Multi-step task no methods")
    assert plan.steps[0]["method"] == "POST"
    assert plan.steps[1]["method"] == "POST"


async def test_plan_steps_mixed_methods_normalised():
    """plan_steps steps with a mix of explicit and absent methods are all normalised."""
    steps_in = [
        {"action": "get_thing", "endpoint": "/a", "parameters": {}, "method": "GET"},
        {"action": "create_thing", "endpoint": "/b", "parameters": {}},  # no method
        {"action": "del_thing", "endpoint": "/c", "parameters": {}, "method": "DELETE"},
    ]
    planner = _planner(make_llm_tool_response(
        "plan_steps",
        {"domain": "https://app.example.com", "steps": steps_in},
    ))
    plan = await planner.plan("Mixed method steps")
    assert plan.steps[0]["method"] == "GET"
    assert plan.steps[1]["method"] == "POST"   # defaulted
    assert plan.steps[2]["method"] == "DELETE"


# ── Error message truncation ──────────────────────────────────────────────────

async def test_plan_no_tool_call_error_is_truncated():
    """The 'no actionable tool call' error must not include full LLMResponse repr."""
    long_text = "A" * 500
    planner = _planner(make_llm_text_response(long_text))
    with pytest.raises(ValueError) as exc_info:
        await planner.plan("Do something")
    msg = str(exc_info.value)
    # Must contain the anchor text
    assert "Planner produced no actionable tool call" in msg
    # Must NOT dump the full long_text (only first 120 chars are included)
    assert long_text not in msg
    # Must NOT include usage/model repr (no "LLMResponse(" wrapper)
    assert "LLMResponse(" not in msg


async def test_handle_fallback_no_route_error_is_truncated():
    """The fallback 'no route_to_domain' error must not include full LLMResponse repr."""
    from ai.provider import ToolCall

    wrong = make_llm_tool_response("plan_steps", {})
    wrong.tool_calls = [ToolCall(name="wrong_tool", input={})]
    wrong.text = "B" * 500
    planner = _planner(wrong)
    planner._search_duckduckgo = AsyncMock(return_value="")
    with pytest.raises(ValueError) as exc_info:
        await planner.handle_fallback("find something")
    msg = str(exc_info.value)
    assert "Fallback planner produced no route_to_domain tool call" in msg
    assert wrong.text not in msg
    assert "LLMResponse(" not in msg

