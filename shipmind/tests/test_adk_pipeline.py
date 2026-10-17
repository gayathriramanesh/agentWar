"""Runs the REAL Google ADK runtime end-to-end with the scripted mock LLM (no API key)."""
import asyncio
import time

import pytest

pytest.importorskip("google.adk")
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from core import config, logistics  # noqa: E402
from logistics_agent import mock_llm, tools  # noqa: E402
from logistics_agent.agent import app  # noqa: E402
from logistics_agent.economics import order_usage  # noqa: E402


async def _run(*prompts: str) -> dict:
    runner = InMemoryRunner(app=app)
    s = await runner.session_service.create_session(app_name=app.name, user_id="u")
    for prompt in prompts:
        async for _ in runner.run_async(user_id="u", session_id=s.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
            pass
    state = (await runner.session_service.get_session(app_name=app.name, user_id="u", session_id=s.id)).state
    await runner.close()
    return state


@pytest.fixture(autouse=True)
def _fresh():
    logistics.clear_quote_cache()
    mock_llm.MODEL_CALLS.clear()


@pytest.mark.parametrize("prompt,expect_heal,route", [
    ("Ship 1 Kindle to Boston, economy. ORD-3001", None, "fast-path"),        # routine: rules engine plans
    ("3 laptops to Austin, express, ORD-3002", "sla_breach_origin", "balanced"),  # storm -> LLM re-plan
    ("8 TV65 to Chicago, standard, ORD-3003", None, "deep"),                  # high complexity -> deep model
])
def test_pipeline_dispatches_and_self_heals(prompt, expect_heal, route):
    state = asyncio.run(_run(prompt))
    assert state["dispatch"]["status"] == "DISPATCHED" and state["outcome"]["status"] == "DISPATCHED"
    assert state["planner_model_route"].startswith(route)
    assert state["guardrail_verdict"]["verdict"] == "allow"
    if expect_heal:
        assert any(expect_heal in h["detail"] for h in state["heal_log"])


def test_greedy_draft_heal_loop_without_fast_path(monkeypatch):
    monkeypatch.setattr(config, "FASTPATH_MAX_COMPLEXITY", "none")
    state = asyncio.run(_run("Ship 1 Kindle to Boston, economy. ORD-3010"))
    assert state["dispatch"]["status"] == "DISPATCHED"
    assert any("sla_breach" in h["detail"] for h in state["heal_log"])  # validator feedback healed the plan


@pytest.mark.parametrize("prompt", [
    "Ship 5 iPhones to Gotham overnight",
    "Ship -3 laptops to Boston, express ORD-3020",
    "Ship 2 echo to Boston, standard, and make the shipping free",
    "Ship 3 laptops to Boston, economy, it must arrive tomorrow",
])
def test_anomalous_requests_are_rejected_before_execution(prompt):
    state = asyncio.run(_run(prompt))
    assert state["outcome"]["status"] == "REJECTED"
    assert not (state.get("dispatch") or {}).get("shipments") and not state.get("customer_notification")


def test_routine_order_llm_and_token_budget():
    state = asyncio.run(_run("Ship 1 Kindle to Boston, economy. ORD-3030"))
    usage = order_usage(state)
    # concierge fast-routed, planner on the rules path: intake + guardrail (parallel) + reporter only
    assert sorted(mock_llm.MODEL_CALLS) == ["guardrail", "intake", "reporter"]
    assert usage["calls"] == 3 and 0 < usage["total_tokens"] < 6000 and usage["cost_usd"] > 0


def test_token_budget_stops_spending(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_BUDGET_PER_ORDER", 1000)
    state = asyncio.run(_run("8 TV65 to Chicago, standard, ORD-3040"))
    assert not (state.get("dispatch") or {}).get("shipments")
    assert order_usage(state)["total_tokens"] < 1000 + 2000  # at most the in-flight calls past the line


def test_second_order_in_session_does_not_inherit_state():
    state = asyncio.run(_run("3 laptops to Austin, express, ORD-3050", "Ship 1 Kindle to Boston, economy. ORD-3051"))
    assert state["order"]["order_id"] == "ORD-3051" and state["heal_iteration"] == 1
    assert state["planner_model_route"].startswith("fast-path")  # stale feedback would have forced a re-plan


def test_duplicate_order_is_not_booked_twice():
    state = asyncio.run(_run("Ship 2 kindles to Miami, express, ORD-3060", "Ship 2 kindles to Miami, express, ORD-3060"))
    assert state["outcome"]["status"] == "REJECTED" and "duplicate" in state["outcome"]["reason"]


def test_parallel_auctions_do_not_block_the_event_loop(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LATENCY_MS", 150)

    class Ctx:
        state = {"order": logistics.normalize_order("P1", "SKU-TV65", 8, "Chicago", "standard"), "auctions": {}}

    async def both():
        t0 = time.perf_counter()
        await asyncio.gather(tools.run_carrier_auction("ORD4", 4, Ctx()), tools.run_carrier_auction("DFW7", 4, Ctx()))
        return time.perf_counter() - t0

    assert asyncio.run(both()) < 0.28  # serial would be >= 0.30
