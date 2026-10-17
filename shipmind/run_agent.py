#!/usr/bin/env python3
"""Run the real ADK multi-agent system on one or more requests and print a live trace.

  python run_agent.py                         # demo requests (mock LLM if no GOOGLE_API_KEY)
  python run_agent.py "Ship 2 kindles to Miami, express, order ORD-2001"
  SHIPMIND_MOCK_LLM=false python run_agent.py # force real Gemini (needs GOOGLE_API_KEY in .env)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import warnings

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)
logging.getLogger("google_adk").setLevel(logging.ERROR)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from core import config, store  # noqa: E402
from logistics_agent.agent import root_agent  # noqa: E402

DEMO = [
    "Ship 1 Kindle to Boston, economy please. Order ORD-2001.",
    "Urgent: 3 laptops to Austin, express, order ORD-2002.",
    "We need 8 TV65 units delivered to Chicago, standard shipping, ORD-2003.",
]


def _short(obj, n=150) -> str:
    s = json.dumps(obj, default=str) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "…"


async def run(prompts: list[str]) -> int:
    mode = "MOCK (scripted LLM, real ADK runtime)" if config.MOCK_LLM else "LIVE Gemini"
    print(f"ShipMind | mode: {mode} | MCP: {config.USE_MCP} | models: {config.MODELS}\n")
    runner = InMemoryRunner(agent=root_agent, app_name="shipmind")
    ok = 0
    try:
        for prompt in prompts:
            session = await runner.session_service.create_session(app_name="shipmind", user_id="ops")
            print("=" * 100 + f"\nUSER: {prompt}\n" + "-" * 100)
            t0 = time.perf_counter()
            async for ev in runner.run_async(user_id="ops", session_id=session.id,
                                             new_message=types.Content(role="user", parts=[types.Part(text=prompt)])):
                ms = (time.perf_counter() - t0) * 1000
                for p in (ev.content.parts if ev.content and ev.content.parts else []):
                    if p.function_call:
                        print(f"[{ms:7.0f}ms] {ev.author:<18} → {p.function_call.name}({_short(p.function_call.args, 110)})")
                    elif p.function_response:
                        print(f"[{ms:7.0f}ms] {ev.author:<18} ← {p.function_response.name}: {_short(p.function_response.response)}")
                    elif p.text and not getattr(p, "thought", False):
                        print(f"[{ms:7.0f}ms] {ev.author:<18} : {_short(p.text.strip(), 400)}")
            state = (await runner.session_service.get_session(
                app_name="shipmind", user_id="ops", session_id=session.id)).state
            d = state.get("dispatch") or {}
            heals = state.get("heal_log") or []
            print("-" * 100)
            print(f"RESULT: {d.get('status', state.get('loop_exit_reason', 'n/a'))} | planner model: "
                  f"{state.get('planner_model_route')} | self-heal events: {len(heals)} | "
                  f"{(time.perf_counter() - t0) * 1000:.0f} ms")
            for h in heals:
                print(f"   ⟲ {h['kind']}: {h['detail']}")
            if config.MOCK_LLM:
                from logistics_agent.mock_llm import MODEL_HEAL_EVENTS
                for m in MODEL_HEAL_EVENTS:
                    print(f"   ⟲ model_fallback: {m}")
                MODEL_HEAL_EVENTS.clear()
            ok += d.get("status") in ("DISPATCHED", "PARTIAL")
    finally:
        await runner.close()
    print(f"\n{ok}/{len(prompts)} orders dispatched.")
    return 0


if __name__ == "__main__":
    store.reset()
    sys.exit(asyncio.run(run(sys.argv[1:] or DEMO)))
