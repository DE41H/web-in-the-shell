from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ai.decision.executor import ExecutionAgent
from ai.discovery.planner import Plan
from conftest import make_llm_text_response, mock_llm_client


# ---- helpers ----

def _ok_response(body=None):
    return SimpleNamespace(
        status_code=200,
        is_success=True,
        json=lambda: body if body is not None else {"id": 1},
    )


def _err_response(status_code=500, text="boom"):
    return SimpleNamespace(
        status_code=status_code,
        is_success=False,
        text=text,
    )


def _build(executor_text, recovery_text=None, dispatch_post=None):
    """Return (dispatch, exec_client, rec_client) mocks for ExecutionAgent."""
    exec_client = mock_llm_client(make_llm_text_response(executor_text))

    rec_text = recovery_text if recovery_text is not None else '```json\n{"x": 2}\n```'
    rec_client = mock_llm_client(make_llm_text_response(rec_text))

    dispatch = MagicMock()
    if dispatch_post is None:
        dispatch.post = AsyncMock(return_value=_ok_response())
    else:
        dispatch.post = AsyncMock(side_effect=dispatch_post)

    return dispatch, exec_client, rec_client


# ---- success / no-retry ----

async def test_execute_success_no_retry():
    dispatch, exec_client, rec_client = _build("refined")
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    assert result.status_code == 200
    assert result.response_body == {"id": 1}
    assert dispatch.post.await_count == 1


# ---- _refine_payload ----

async def test_execute_refine_payload_uses_json_block():
    dispatch, exec_client, rec_client = _build('```json\n{"x": 99, "y": "new"}\n```')
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert dispatch.post.await_args.args[1] == {"x": 99, "y": "new"}


async def test_execute_refine_payload_falls_back_to_original():
    dispatch, exec_client, rec_client = _build("refined, no json here")
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert dispatch.post.await_args.args[1] == {"x": 1}


# ---- retry behavior ----

async def test_execute_retry_with_revised_params():
    dispatch, exec_client, rec_client = _build(
        "refined",
        recovery_text='```json\n{"x": 99}\n```',
        dispatch_post=[_err_response(500, "first fail"), _ok_response({"ok": True})],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    assert result.status_code == 200
    assert result.response_body == {"ok": True}
    assert dispatch.post.await_count == 2
    assert dispatch.post.await_args_list[1].args[1] == {"x": 99}


async def test_execute_recovery_abort_returns_failure():
    dispatch, exec_client, rec_client = _build(
        "refined",
        recovery_text="ABORT: bad token",
        dispatch_post=[_err_response(500, "fail")],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is False
    assert result.error == "bad token"
    assert dispatch.post.await_count == 1


async def test_execute_exhausts_retries():
    dispatch, exec_client, rec_client = _build(
        "refined",
        recovery_text='```json\n{"x": 2}\n```',
        dispatch_post=[
            _err_response(500, "fail1"),
            _err_response(500, "fail2"),
            _err_response(500, "fail3"),
        ],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is False
    assert result.error == "Exhausted 3 retries."
    assert dispatch.post.await_count == 3


# ---- non-json success body ----

async def test_execute_non_json_response_body():
    def _raise_json():
        raise ValueError("not json")

    bad_body_response = SimpleNamespace(
        status_code=200,
        is_success=True,
        json=_raise_json,
    )

    dispatch, exec_client, rec_client = _build(
        "refined",
        dispatch_post=[bad_body_response],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    assert result.response_body is None


# ---- execute_plan ----

async def test_execute_plan_empty_steps_falls_back_to_top_level():
    dispatch, exec_client, rec_client = _build("refined")
    plan = Plan(
        target_domain="https://app.example.com",
        target_endpoints=["/posts"],
        action="create",
        parameters={"x": 1},
        steps=[],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    results = await agent.execute_plan(plan)
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].endpoint == "/posts"
    assert dispatch.post.await_count == 1


async def test_execute_plan_multi_step():
    dispatch, exec_client, rec_client = _build("refined")
    plan = Plan(
        target_domain="https://app.example.com",
        target_endpoints=["/x", "/y"],
        action="create",
        parameters={"x": 1},
        steps=[
            {"action": "create", "endpoint": "/x", "parameters": {"a": 1}},
            {"action": "update", "endpoint": "/y", "parameters": {"b": 2}},
        ],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    results = await agent.execute_plan(plan)
    assert len(results) == 2
    assert results[0].success is True
    assert results[1].success is True
    assert dispatch.post.await_count == 2
    assert dispatch.post.await_args_list[0].args[0] == "/x"
    assert dispatch.post.await_args_list[1].args[0] == "/y"


async def test_execute_plan_stops_on_failure():
    dispatch, exec_client, rec_client = _build(
        "refined",
        recovery_text="ABORT: bad input",
        dispatch_post=[_err_response(500, "fail")],
    )
    plan = Plan(
        target_domain="https://app.example.com",
        target_endpoints=["/x", "/y"],
        action="create",
        parameters={"x": 1},
        steps=[
            {"action": "create", "endpoint": "/x", "parameters": {"a": 1}},
            {"action": "update", "endpoint": "/y", "parameters": {"b": 2}},
        ],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    results = await agent.execute_plan(plan)
    assert len(results) == 1
    assert results[0].success is False
    assert dispatch.post.await_count == 1


async def test_execute_plan_compat_top_level_when_no_steps():
    dispatch, exec_client, rec_client = _build("refined")
    plan = Plan(
        target_domain="https://app.example.com",
        target_endpoints=["/a"],
        action="legacy_action",
        parameters={"legacy": True},
        steps=[],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    results = await agent.execute_plan(plan)
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].endpoint == "/a"
    assert dispatch.post.await_args.args[1] == {"legacy": True}


# ---- edge cases ----

async def test_execute_plan_with_initial_state():
    from serialization.models import CompactStateModel
    dispatch, exec_client, rec_client = _build("refined")
    initial = CompactStateModel(endpoint="/init", status_code=200, payload={"key": "value"})
    plan = Plan(
        target_domain="https://app.example.com",
        target_endpoints=["/a"],
        action="do",
        parameters={},
        steps=[],
    )
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    results = await agent.execute_plan(plan, state=initial)
    assert len(results) == 1
    assert results[0].success is True


async def test_execute_refine_payload_invalid_json_falls_back():
    dispatch, exec_client, rec_client = _build("```json\n{not valid json}\n```")
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert dispatch.post.await_args.args[1] == {"x": 1}


# ── HTTP method routing ──────────────────────────────────────────────────────

async def test_execute_get_method_calls_dispatch_get():
    """method="GET" routes to dispatch.get(), not post()."""
    dispatch, exec_client, rec_client = _build('```json\n{}\n```')
    dispatch.get = AsyncMock(return_value=_ok_response({"posts": []}))
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="list", endpoint="/posts", parameters={}, method="GET")
    assert result.success is True
    dispatch.get.assert_awaited_once_with("/posts")
    dispatch.post.assert_not_awaited()

async def test_execute_put_method_calls_dispatch_put():
    dispatch, exec_client, rec_client = _build('```json\n{"title": "x"}\n```')
    dispatch.put = AsyncMock(return_value=_ok_response())
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(
        action="update", endpoint="/posts/1",
        parameters={"title": "x"}, method="PUT",
    )
    assert result.success is True
    dispatch.put.assert_awaited_once()
    dispatch.post.assert_not_awaited()

async def test_execute_patch_method_calls_dispatch_patch():
    dispatch, exec_client, rec_client = _build('```json\n{"title": "y"}\n```')
    dispatch.patch = AsyncMock(return_value=_ok_response())
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(
        action="patch", endpoint="/posts/1",
        parameters={"title": "y"}, method="PATCH",
    )
    assert result.success is True
    dispatch.patch.assert_awaited_once()
    dispatch.post.assert_not_awaited()

async def test_execute_default_method_is_post():
    """method defaults to POST when not supplied."""
    dispatch, exec_client, rec_client = _build('```json\n{"x": 1}\n```')
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    dispatch.post.assert_awaited_once()

async def test_execute_case_insensitive_method():
    """method="get" (lowercase) still routes to dispatch.get()."""
    dispatch, exec_client, rec_client = _build('```json\n{}\n```')
    dispatch.get = AsyncMock(return_value=_ok_response())
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="list", endpoint="/posts", parameters={}, method="get")
    assert result.success is True
    dispatch.get.assert_awaited_once()

# ── Dispatch exception wrapping ──────────────────────────────────────────────

async def test_execute_dispatch_exception_returns_failure_result():
    """httpx.ConnectError or any dispatch exception returns ExecutionResult(success=False)."""
    import httpx
    dispatch, exec_client, rec_client = _build('```json\n{"x": 1}\n```')
    dispatch.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is False
    assert result.status_code == 0
    assert "ConnectError" in result.error or "Dispatch error" in result.error

async def test_execute_generic_dispatch_exception_wraps_cleanly():
    dispatch, exec_client, rec_client = _build('```json\n{"x": 1}\n```')
    dispatch.post = AsyncMock(side_effect=RuntimeError("timeout"))
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is False
    assert result.status_code == 0

# ── execute_plan passes method ────────────────────────────────────────────────

async def test_execute_plan_passes_method_from_step():
    """execute_plan reads 'method' from each step dict and forwards it to execute()."""
    from ai.discovery.planner import Plan
    dispatch, exec_client, rec_client = _build('```json\n{}\n```')
    dispatch.get = AsyncMock(return_value=_ok_response({"posts": []}))
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    plan = Plan(
        target_domain="https://example.com",
        target_endpoints=["/posts"],
        action="list",
        parameters={},
        steps=[{"action": "list", "endpoint": "/posts", "parameters": {}, "method": "GET"}],
    )
    results = await agent.execute_plan(plan)
    assert results[0].success is True
    dispatch.get.assert_awaited_once()

async def test_execute_plan_defaults_to_post_when_method_absent():
    """Steps without 'method' key default to POST."""
    from ai.discovery.planner import Plan
    dispatch, exec_client, rec_client = _build('```json\n{"x": 1}\n```')
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    plan = Plan(
        target_domain="https://example.com",
        target_endpoints=["/posts"],
        action="create",
        parameters={"x": 1},
        steps=[{"action": "create", "endpoint": "/posts", "parameters": {"x": 1}}],
    )
    results = await agent.execute_plan(plan)
    assert results[0].success is True
    dispatch.post.assert_awaited_once()

# ── Exponential backoff ───────────────────────────────────────────────────────

async def test_execute_backoff_called_on_429(monkeypatch):
    """asyncio.sleep is awaited with a positive delay when status is 429."""
    import asyncio as _aio
    slept = []
    async def fake_sleep(n):
        slept.append(n)
    monkeypatch.setattr(_aio, "sleep", fake_sleep)

    dispatch, exec_client, rec_client = _build(
        '```json\n{"x": 1}\n```',
        recovery_text='```json\n{"x": 2}\n```',
    )
    # First call returns 429, second returns success
    dispatch.post = AsyncMock(side_effect=[
        _err_response(429, "rate limited"),
        _ok_response(),
    ])
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    assert any(s > 0 for s in slept), "Expected at least one sleep call"

async def test_execute_backoff_called_on_503(monkeypatch):
    import asyncio as _aio
    slept = []
    async def fake_sleep(n):
        slept.append(n)
    monkeypatch.setattr(_aio, "sleep", fake_sleep)

    dispatch, exec_client, rec_client = _build(
        '```json\n{"x": 1}\n```',
        recovery_text='```json\n{"x": 2}\n```',
    )
    dispatch.post = AsyncMock(side_effect=[
        _err_response(503, "service unavailable"),
        _ok_response(),
    ])
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    result = await agent.execute(action="create", endpoint="/posts", parameters={"x": 1})
    assert result.success is True
    assert any(s > 0 for s in slept)


# ── GET optimization: skip LLM refinement ────────────────────────────────────

async def test_execute_get_does_not_call_llm_refinement():
    """GET requests skip _refine_payload — the LLM client must not be called."""
    dispatch, exec_client, rec_client = _build('```json\n{}\n```')
    dispatch.get = AsyncMock(return_value=_ok_response({"posts": []}))
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="list", endpoint="/posts", parameters={}, method="GET")
    exec_client.chat.assert_not_awaited()


async def test_execute_get_lowercase_does_not_call_llm_refinement():
    """method='get' (lowercase) also skips LLM refinement."""
    dispatch, exec_client, rec_client = _build('```json\n{}\n```')
    dispatch.get = AsyncMock(return_value=_ok_response())
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="list", endpoint="/posts", parameters={}, method="get")
    exec_client.chat.assert_not_awaited()


async def test_execute_post_still_calls_llm_refinement():
    """POST requests must still call _refine_payload (regression guard)."""
    dispatch, exec_client, rec_client = _build('```json\n{"title": "refined"}\n```')
    agent = ExecutionAgent(dispatch, exec_client, rec_client)
    await agent.execute(action="create", endpoint="/posts", parameters={"title": "orig"})
    exec_client.chat.assert_awaited_once()
