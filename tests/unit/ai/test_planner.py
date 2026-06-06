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


def test_plan_default_fallback_search_false():
    plan = Plan(target_domain="x", target_endpoints=["a"], action="b", parameters={})
    assert plan.fallback_search is False


def test_plan_default_search_query_empty():
    plan = Plan(target_domain="x", target_endpoints=["a"], action="b", parameters={})
    assert plan.search_query == ""


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
    assert plan.steps == steps_in
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
    assert plan.fallback_search is False
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
    assert plan.fallback_search is False


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
    assert len(planner.last_messages) == 1
    assert planner.last_messages[0]["role"] == "user"


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
    assert len(sent) == 3
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
    assert len(sent) == 1
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

    assert len(planner.last_messages) == 2
    assert planner.last_messages[0]["role"] == "user"
    assert planner.last_messages[1] == {
        "role": "assistant",
        "content": "I will create the user now.",
    }


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

    assert planner.last_messages[-1]["role"] == "user"
    assert len(planner.last_messages) == 1


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

