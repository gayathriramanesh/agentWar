"""Runs the REAL Google ADK runtime end-to-end with the scripted mock LLM (no API key)."""
import asyncio

import pytest

pytest.importorskip("google.adk")
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from logistics_agent.agent import root_agent  # noqa: E402


async def _run(prompt: str) -> dict:
    runner = InMemoryRunner(agent=root_agent, app_name="t")
    s = await runner.session_service.create_session(app_name="t", user_id="u")
    async for _ in runner.run_async(user_id="u", session_id=s.id,
                                    new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
        pass
    state = (await runner.session_service.get_session(app_name="t", user_id="u", session_id=s.id)).state
    await runner.close()
    return state


@pytest.mark.parametrize("prompt,expect_heal,route", [
    ("Ship 1 Kindle to Boston, economy. ORD-3001", "sla_breach", "balanced"),
    ("3 laptops to Austin, express, ORD-3002", "sla_breach_origin", "balanced"),
    ("8 TV65 to Chicago, standard, ORD-3003", None, "deep"),
])
def test_pipeline_dispatches_and_self_heals(prompt, expect_heal, route):
    state = asyncio.run(_run(prompt))
    assert state["dispatch"]["status"] == "DISPATCHED"
    assert state["planner_model_route"].startswith(route)
    if expect_heal:
        assert any(expect_heal in h["detail"] for h in state["heal_log"])
