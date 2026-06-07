from __future__ import annotations

from ai.decision.answer import AnswerAgent
from ai.decision.executor import ExecutionResult
from conftest import make_llm_text_response, make_llm_empty_response, mock_llm_client


def _result(body=None, status=200, success=True):
    return ExecutionResult(
        success=success,
        endpoint="/test",
        status_code=status,
        response_body=body,
    )


# ── ANSWER: prefix ────────────────────────────────────────────────────────────

async def test_answer_prefix_returns_satisfied():
    client = mock_llm_client(
        make_llm_text_response("ANSWER: The scientific name is Carassius auratus.")
    )
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize(
        "scientific name of goldfish", [_result({"name": "goldfish"})]
    )
    assert satisfied is True
    assert text == "The scientific name is Carassius auratus."


async def test_answer_prefix_case_insensitive():
    client = mock_llm_client(
        make_llm_text_response("answer: Paris is the capital of France.")
    )
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize(
        "capital of France", [_result({"capital": "Paris"})]
    )
    assert satisfied is True
    assert text == "Paris is the capital of France."


# ── RETRY: prefix ─────────────────────────────────────────────────────────────

async def test_retry_prefix_returns_not_satisfied():
    client = mock_llm_client(
        make_llm_text_response("RETRY: matchType=NONE, species not found.")
    )
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize(
        "scientific name of cheetah", [_result({"matchType": "NONE"})]
    )
    assert satisfied is False
    assert text == "matchType=NONE, species not found."


async def test_retry_prefix_case_insensitive():
    client = mock_llm_client(make_llm_text_response("retry: empty results list"))
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize(
        "find user", [_result({"results": []})]
    )
    assert satisfied is False
    assert "empty" in text


# ── Fallback when neither prefix ──────────────────────────────────────────────

async def test_no_prefix_trusts_http_success():
    """When the model returns prose without a prefix, trust HTTP 200."""
    client = mock_llm_client(
        make_llm_text_response(
            "The goldfish scientific name is Carassius auratus."
        )
    )
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize(
        "scientific name of goldfish", [_result({"name": "goldfish"})]
    )
    assert satisfied is True
    assert "Carassius" in text


async def test_empty_llm_response_returns_success():
    """Empty model output → optimistic trust of HTTP 200."""
    client = mock_llm_client(make_llm_empty_response())
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize("some intent", [_result({"ok": True})])
    assert satisfied is True
    assert text  # non-empty fallback message


# ── Prompt construction ───────────────────────────────────────────────────────

async def test_empty_response_body_handled():
    """ExecutionResult with response_body=None should not crash."""
    client = mock_llm_client(make_llm_text_response("ANSWER: done."))
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize("do something", [_result(body=None)])
    assert satisfied is True


async def test_multiple_results_all_included():
    """All step bodies are included in the prompt."""
    client = mock_llm_client(make_llm_text_response("ANSWER: combined."))
    agent = AnswerAgent(client)
    results = [_result({"step": 1}), _result({"step": 2})]
    await agent.synthesize("multi-step intent", results)
    call_kwargs = client.chat.call_args
    prompt = call_kwargs[1]["messages"][0]["content"]
    assert '"step": 1' in prompt
    assert '"step": 2' in prompt


# ── LLM failure fallback ──────────────────────────────────────────────────────

async def test_llm_exception_returns_optimistic_success():
    """If the LLM call raises, return (True, fallback message) — don't crash pipeline."""
    client = mock_llm_client(side_effect=RuntimeError("network down"))
    agent = AnswerAgent(client)
    satisfied, text = await agent.synthesize("any intent", [_result({"data": 1})])
    assert satisfied is True
    assert text
