"""Runs eval cases through the real ADK app and scores them. Used by run_evals.py and the test suite."""
from __future__ import annotations

import copy
import time

from google.adk.runners import InMemoryRunner
from google.genai import types

from core import data, logistics
from core.carrier_apis import Chaos
from core.healing import BREAKER

from . import metrics


async def run_case(runner: InMemoryRunner, case: dict) -> tuple[dict, str, float]:
    """One conversation in a fresh session. Returns (final state, the user's words, last-turn latency)."""
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="eval")
    for turn in case["turns"]:
        t0 = time.perf_counter()  # latency of the final turn (earlier turns are setup, e.g. the first order)
        async for _ in runner.run_async(user_id="eval", session_id=session.id,
                                        new_message=types.Content(role="user", parts=[types.Part(text=turn)])):
            pass
    latency_ms = (time.perf_counter() - t0) * 1000  # noqa: F821 (turns is non-empty)
    state = (await runner.session_service.get_session(app_name=runner.app_name, user_id="eval",
                                                      session_id=session.id)).state
    return dict(state), "\n".join(case["turns"][-3:]), latency_ms


async def run_suite(cases: list[dict], judge=None) -> dict:
    from logistics_agent.agent import app
    from logistics_agent.economics import order_usage

    runner = InMemoryRunner(app=app)
    rows = []
    try:
        for case in cases:
            snapshot = copy.deepcopy(data.INVENTORY)
            Chaos.configure(case.get("chaos", 0.0), 42)
            BREAKER.reset()
            logistics.clear_quote_cache()
            try:
                state, user_text, latency = await run_case(runner, case)
                row = metrics.score_case(case, state, user_text, order_usage(state), latency)
                if judge is not None:
                    row["judge"] = judge(case, state)
                rows.append(row)
            finally:
                data.INVENTORY.clear()
                data.INVENTORY.update(snapshot)
                Chaos.configure(0.0, 42)
    finally:
        await runner.close()
    summary = metrics.aggregate(rows)
    return {"summary": summary, "gate_failures": metrics.gate(summary), "cases": rows}
