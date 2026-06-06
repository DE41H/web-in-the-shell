from types import SimpleNamespace

from ai.decision.recovery import RecoveryAgent
from conftest import make_llm_text_response, make_llm_empty_response, mock_llm_client


def _resp(status_code=500, text="err"):
    return SimpleNamespace(status_code=status_code, text=text)


class _Unreadable:
    status_code = 500

    @property
    def text(self):
        raise RuntimeError("cannot decode")


# ---- abort branch ----

async def test_recovery_abort_response():
    client = mock_llm_client(make_llm_text_response("ABORT: invalid token"))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.revised_parameters == {}
    assert result.abort_reason == "invalid token"


async def test_recovery_abort_with_extra_text():
    client = mock_llm_client(make_llm_text_response("ABORT: rate limited. try later."))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.abort_reason == "rate limited. try later."


async def test_recovery_abort_case_insensitive():
    client = mock_llm_client(make_llm_text_response("abort: nope"))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.abort_reason == "nope"


# ---- json block branch ----

async def test_recovery_json_block():
    client = mock_llm_client(make_llm_text_response('```json\n{"foo": "bar"}\n```'))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is True
    assert result.revised_parameters == {"foo": "bar"}


async def test_recovery_json_block_with_surrounding_text():
    client = mock_llm_client(
        make_llm_text_response('Here you go:\n```json\n{"a": 1}\n```\nGood luck.')
    )
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is True
    assert result.revised_parameters == {"a": 1}


# ---- unparseable fallback ----

async def test_recovery_unparseable_json():
    client = mock_llm_client(make_llm_text_response("```json\n{invalid json}\n```"))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.revised_parameters == {}
    assert result.abort_reason == "Recovery agent returned unparseable output."


async def test_recovery_no_json_no_abort():
    client = mock_llm_client(make_llm_text_response("I'm not sure what to do."))
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.abort_reason == "Recovery agent returned unparseable output."


async def test_recovery_empty_content():
    client = mock_llm_client(make_llm_empty_response())
    agent = RecoveryAgent(client)
    result = await agent.handle(_resp(), {}, "create_user")
    assert result.retry is False
    assert result.abort_reason == "Recovery agent returned unparseable output."


# ---- error body handling ----

async def test_recovery_truncates_long_error_body():
    long_text = "x" * 3000
    failed = _resp(status_code=500, text=long_text)
    client = mock_llm_client(make_llm_text_response("ok"))
    agent = RecoveryAgent(client)
    await agent.handle(failed, {}, "create_user")

    content = client.chat.call_args.kwargs["messages"][0]["content"]
    idx = content.index("Error body:\n") + len("Error body:\n")
    err_body = content[idx:]
    assert err_body == "x" * 800
    assert len(err_body) == 800


async def test_recovery_handles_unreadable_response():
    failed = _Unreadable()
    client = mock_llm_client(make_llm_text_response("ok"))
    agent = RecoveryAgent(client)
    await agent.handle(failed, {}, "create_user")

    content = client.chat.call_args.kwargs["messages"][0]["content"]
    assert "(response body unreadable)" in content


# ── ABORT parsing edge cases ─────────────────────────────────────────────────

async def test_abort_without_colon_uses_fallback_reason():
    """'ABORT' with no colon should not raise IndexError; reason should be empty or fallback."""
    from conftest import mock_llm_client, make_llm_text_response
    from ai.decision.recovery import RecoveryAgent
    import httpx

    client = mock_llm_client(make_llm_text_response("ABORT:"))
    agent = RecoveryAgent(client)
    failed = httpx.Response(400, text="bad")
    result = await agent.handle(failed, {}, "test")
    # "ABORT:" with empty reason — should not crash
    assert result.retry is False

async def test_abort_just_word_no_colon():
    """'ABORT' with no colon at all should not raise IndexError."""
    from conftest import mock_llm_client, make_llm_text_response
    from ai.decision.recovery import RecoveryAgent
    import httpx

    client = mock_llm_client(make_llm_text_response("ABORT"))
    agent = RecoveryAgent(client)
    failed = httpx.Response(400, text="bad")
    # Should not raise — abort_reason may be empty string or the fallback
    result = await agent.handle(failed, {}, "test")
    assert result.retry is False


# ── M2: error_body and parameters sanitization ────────────────────────────────

async def test_recovery_error_body_is_sanitized():
    """M2: error_body is passed through sanitize_for_llm before embedding in prompt."""
    from unittest.mock import patch as _patch

    sanitized_calls = []

    def tracking_sanitize(text: str) -> str:
        sanitized_calls.append(text)
        return text

    client = mock_llm_client(make_llm_text_response('```json\n{"x": 1}\n```'))
    agent = RecoveryAgent(client)
    failed = _resp(status_code=500, text="error: <script>alert(1)</script>")

    with _patch("ai.decision.recovery.sanitize_for_llm", side_effect=tracking_sanitize):
        await agent.handle(failed, {"key": "val"}, "create_user")

    # sanitize_for_llm must have been called with the error body text
    error_body_calls = [c for c in sanitized_calls if "script" in c or "error:" in c]
    assert error_body_calls, (
        f"sanitize_for_llm not called with error body. Calls: {sanitized_calls}"
    )


async def test_recovery_parameters_are_sanitized():
    """M2: original_parameters JSON is passed through sanitize_for_llm."""
    from unittest.mock import patch as _patch

    sanitized_calls = []

    def tracking_sanitize(text: str) -> str:
        sanitized_calls.append(text)
        return text

    client = mock_llm_client(make_llm_text_response('```json\n{"x": 1}\n```'))
    agent = RecoveryAgent(client)
    failed = _resp(status_code=500, text="err")

    with _patch("ai.decision.recovery.sanitize_for_llm", side_effect=tracking_sanitize):
        await agent.handle(failed, {"secret_key": "value123"}, "create_user")

    # sanitize_for_llm must have been called with the JSON-serialized parameters
    param_calls = [c for c in sanitized_calls if "secret_key" in c or "value123" in c]
    assert param_calls, f"sanitize_for_llm not called with parameters. Calls: {sanitized_calls}"


# ── attempt_number in prompt ──────────────────────────────────────────────────

async def test_recovery_attempt_number_in_prompt_first_attempt():
    """M3 token reduction: attempt_number=0 results in 'Attempt: 1' in the prompt."""
    client = mock_llm_client(make_llm_text_response('```json\n{"x": 1}\n```'))
    agent = RecoveryAgent(client)
    failed = _resp(status_code=500, text="err")
    await agent.handle(failed, {}, "create_user", attempt_number=0)

    content = client.chat.call_args.kwargs["messages"][0]["content"]
    assert "Attempt: 1" in content


async def test_recovery_attempt_number_in_prompt_second_attempt():
    """attempt_number=1 results in 'Attempt: 2' in the prompt."""
    client = mock_llm_client(make_llm_text_response('```json\n{"x": 1}\n```'))
    agent = RecoveryAgent(client)
    failed = _resp(status_code=500, text="err")
    await agent.handle(failed, {}, "create_user", attempt_number=1)

    content = client.chat.call_args.kwargs["messages"][0]["content"]
    assert "Attempt: 2" in content


async def test_recovery_default_attempt_number_is_zero():
    """Default attempt_number=0 when not provided — 'Attempt: 1' in prompt."""
    client = mock_llm_client(make_llm_text_response('```json\n{"x": 1}\n```'))
    agent = RecoveryAgent(client)
    failed = _resp(status_code=500, text="err")
    await agent.handle(failed, {}, "create_user")  # no attempt_number kwarg

    content = client.chat.call_args.kwargs["messages"][0]["content"]
    assert "Attempt: 1" in content
