"""ShipMind: autonomous, self-healing logistics control tower on Google ADK.

shipmind_concierge (conversational, MCP tools; regex screen + fast route for obvious new orders)
 └── fulfillment_pipeline (Sequential; resets per-order state, screens input)
      ├── intake_and_guardrail (Parallel)
      │    ├── intake_agent                 fast model    -> order (validated + grounded in the user's words)
      │    └── input_guardrail_agent        fast model    -> allow | clarify | block (structured verdict)
      └── order_execution (Sequential, gated: REJECTED unless intake succeeded AND guardrail allowed)
           ├── parallel_recon (Parallel)
           │    ├── inventory_agent         deterministic -> allocation
           │    └── risk_agent              deterministic -> disruptions
           ├── plan_validate_heal_loop (Loop, max N)
           │    ├── planner_agent           rules fast path (low/medium) or routed model -> auctions + plan
           │    └── validator_agent         deterministic -> validate, dispatch (fail-over), exit or feedback
           └── reporter_agent               fast model    -> notify customer + ops report
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent  # noqa: E402
from google.adk.agents.context_cache_config import ContextCacheConfig  # noqa: E402
from google.adk.apps import App  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from core import config, logistics  # noqa: E402

from . import prompts, tools  # noqa: E402
from .model_router import model_for, route_by_complexity, routed_planner_model  # noqa: E402
from .economics import enforce_budget, record_usage  # noqa: E402
from .workflow_agents import (GuardrailVerdict, InventoryAgent, RiskAgent, ValidatorAgent,  # noqa: E402
                              fast_path_plan, fast_route, require_valid_order, screen_input, start_order)

_LOCAL = {fn.__name__: fn for fn in logistics.DATA_TOOLS}


def data_tools(names: list[str]) -> list:
    """Warehouse/inventory tools via the MCP server; self-heals to in-process tools if MCP is unavailable."""
    if config.USE_MCP:
        try:
            from google.adk.tools import McpToolset
            from google.adk.tools.mcp_tool import StdioConnectionParams
            from mcp import StdioServerParameters

            return [McpToolset(
                connection_params=StdioConnectionParams(
                    server_params=StdioServerParameters(command=sys.executable,
                                                        args=[str(ROOT / "mcp_server" / "server.py")]),
                    timeout=20),
                tool_filter=names)]
        except Exception as e:  # noqa: BLE001
            print(f"[shipmind] MCP unavailable ({e}); using in-process tools", file=sys.stderr)
    return [FunctionTool(_LOCAL[n]) for n in names]


def gen_config(temperature: float) -> types.GenerateContentConfig:
    """Low temperature for extraction/planning; a hard per-call timeout so a hung model can't stall an order."""
    return types.GenerateContentConfig(temperature=temperature,
                                       http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_S * 1000))


# Every LLM agent: token budget check before the call, usage/cost accounting after it.
_ECON = dict(before_model_callback=enforce_budget, after_model_callback=record_usage)

intake_agent = LlmAgent(
    name="intake_agent", model=model_for("fast"), instruction=prompts.intake_instruction,
    include_contents="none",  # user's recent words are in the instruction; no session history
    tools=[tools.save_order], generate_content_config=gen_config(0.0),
    after_tool_callback=tools.heal_tracker, **_ECON)

input_guardrail_agent = LlmAgent(
    name="input_guardrail_agent", model=model_for("fast"), instruction=prompts.guardrail_instruction,
    description="Independent LLM judge of the request: allow, clarify or block.",
    include_contents="none", output_schema=GuardrailVerdict, output_key="guardrail_verdict",
    disallow_transfer_to_parent=True, disallow_transfer_to_peers=True,
    generate_content_config=gen_config(0.0), **_ECON)

planner_agent = LlmAgent(
    name="planner_agent", model=routed_planner_model(), instruction=prompts.planner_instruction,
    include_contents="none",  # everything it needs is in CONTEXT_JSON; no ever-growing history per call
    before_agent_callback=fast_path_plan,  # routine orders: rules engine, 0 LLM calls
    before_model_callback=[enforce_budget, route_by_complexity], after_model_callback=record_usage,
    generate_content_config=gen_config(0.0),
    # Hot path stays in-process (no IPC hop) to keep latency minimal.
    tools=[tools.reallocate_inventory, tools.run_carrier_auction, tools.submit_plan],
    output_key="plan_summary", after_tool_callback=tools.heal_tracker)

reporter_agent = LlmAgent(
    name="reporter_agent", model=model_for("fast"), instruction=prompts.reporter_instruction,
    include_contents="none", generate_content_config=gen_config(0.3),
    tools=[tools.notify_customer], after_tool_callback=tools.heal_tracker, **_ECON)

order_execution = SequentialAgent(
    name="order_execution", before_agent_callback=require_valid_order,
    sub_agents=[
        ParallelAgent(name="parallel_recon", sub_agents=[InventoryAgent(name="inventory_agent"),
                                                         RiskAgent(name="risk_agent")]),
        LoopAgent(name="plan_validate_heal_loop", max_iterations=config.MAX_HEAL_ITERATIONS,
                  sub_agents=[planner_agent,
                              ValidatorAgent(name="validator_agent", max_iterations=config.MAX_HEAL_ITERATIONS)]),
        reporter_agent,
    ])

fulfillment_pipeline = SequentialAgent(
    name="fulfillment_pipeline", before_agent_callback=start_order,
    description="End-to-end fulfillment for a new order: intake, parallel recon, plan/validate/heal loop, report.",
    sub_agents=[ParallelAgent(name="intake_and_guardrail", sub_agents=[intake_agent, input_guardrail_agent]),
                order_execution])

root_agent = LlmAgent(
    name="shipmind_concierge", model=model_for("balanced"), instruction=prompts.CONCIERGE,
    description="Conversational logistics control tower.", generate_content_config=gen_config(0.2),
    before_agent_callback=screen_input,
    before_model_callback=[enforce_budget, fast_route], after_model_callback=record_usage,
    tools=data_tools(["track_shipment", "check_inventory", "get_disruptions", "list_warehouses"]),
    sub_agents=[fulfillment_pipeline])

# Context caching (live Gemini only): each agent keeps its own cached prefix (system prompt + tool
# declarations), so repeated orders pay the discounted cached-input rate instead of full price.
if config.MOCK_LLM:
    app = App(name="logistics_agent", root_agent=root_agent)
else:
    app = App(name="logistics_agent", root_agent=root_agent,
              context_cache_config=ContextCacheConfig(min_tokens=2048, ttl_seconds=1800, cache_intervals=50))
