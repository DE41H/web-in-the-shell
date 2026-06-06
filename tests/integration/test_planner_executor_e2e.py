import pytest
import respx
import httpx

from ai.discovery.planner import PlannerAgent
from ai.decision.executor import ExecutionAgent
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, tool_call):
        self._tool_call = tool_call
        self.provider = "openai"

    async def chat(self, *args, **kwargs):
        # Return an object with .tool_calls, .text, .usage attributes
        return SimpleNamespace(tool_calls=[self._tool_call], text="", usage={})


@pytest.mark.integration
async def test_planner_executor_with_mocked_llm(tmp_path):
    # Planner + Executor flow: planner creates steps, executor sends them.
    # We'll mock network endpoints via respx and use a fake LLM provider.
    with respx.mock:
        respx.get("https://api.test/posts").mock(return_value=httpx.Response(200, json=[{"id":1}]))
        respx.post("https://api.test/posts").mock(return_value=httpx.Response(201, json={"id":2}))

        # The tool call instructs the planner to target api.test with two steps
        tool_call = SimpleNamespace(name="plan_steps", input={
            "domain": "https://api.test",
            "steps": [
                {
                    "action": "list_posts",
                    "endpoint": "/posts",
                    "parameters": {},
                    "method": "GET",
                },
                {
                    "action": "create_post",
                    "endpoint": "/posts",
                    "parameters": {"title": "x"},
                    "method": "POST",
                },
            ],
        })

        fake = FakeLLM(tool_call)
        planner = PlannerAgent(fake)
        plan = await planner.plan("Fetch and create post", "")

        # ExecutionAgent needs a dispatch client and two LLM clients
        # (one for refine, one for recovery)
        from network.session.manager import SessionManager
        from network.dispatch.client import DispatchClient

        session = SessionManager()
        async with DispatchClient(session, base_url="https://api.test") as client:
            # Dummy LLM for executor refine/recovery that returns bare JSON payloads
            class RefineLLM:
                async def chat(self, *a, **k):
                    return SimpleNamespace(
                        tool_calls=[], text='```json {"title": "x"} ```', usage={}
                    )

            exec_agent = ExecutionAgent(
                dispatch=client, client=RefineLLM(), recovery_client=RefineLLM()
            )
            results = await exec_agent.execute_plan(plan)
            assert isinstance(results, list)
            assert len(results) == 2
            assert results[0].success is True
            assert results[1].success is True
