"""ShipMind: autonomous, self-healing logistics control tower on Google ADK.

shipmind_concierge (conversational, MCP tools)
 └── fulfillment_pipeline (Sequential)
      ├── intake_agent                      fast model    -> order + complexity
      ├── parallel_recon (Parallel)
      │    ├── inventory_agent              fast model    -> MCP allocate_inventory
      │    └── risk_agent                   fast model    -> MCP get_disruptions
      ├── plan_validate_heal_loop (Loop, max N)
      │    ├── planner_agent                routed model  -> carrier auction (parallel APIs) + plan
      │    └── validator_agent              validator     -> validate, dispatch (fail-over), exit/feedback
      └── reporter_agent                    fast model    -> notify customer + ops report
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402

from core import config, logistics  # noqa: E402

from . import prompts, tools  # noqa: E402
from .model_router import model_for, route_by_complexity, routed_planner_model  # noqa: E402

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


intake_agent = LlmAgent(
    name="intake_agent", model=model_for("fast"), instruction=prompts.INTAKE,
    tools=[tools.save_order], output_key="order_summary", after_tool_callback=tools.heal_tracker)

inventory_agent = LlmAgent(
    name="inventory_agent", model=model_for("fast"), instruction=prompts.INVENTORY,
    tools=data_tools(["allocate_inventory"]), output_key="inventory_report")

risk_agent = LlmAgent(
    name="risk_agent", model=model_for("fast"), instruction=prompts.RISK,
    tools=data_tools(["get_disruptions"]), output_key="risk_report")

planner_agent = LlmAgent(
    name="planner_agent", model=routed_planner_model(), instruction=prompts.PLANNER,
    before_model_callback=route_by_complexity,
    # Hot path stays in-process (no IPC hop) to keep latency minimal.
    tools=[FunctionTool(logistics.allocate_inventory), tools.run_carrier_auction, tools.submit_plan],
    output_key="plan_summary", after_tool_callback=tools.heal_tracker)

validator_agent = LlmAgent(
    name="validator_agent", model=model_for("validator"), instruction=prompts.VALIDATOR,
    tools=[tools.validate_plan, tools.dispatch_shipment, tools.exit_loop],
    output_key="feedback", after_tool_callback=tools.heal_tracker)

reporter_agent = LlmAgent(
    name="reporter_agent", model=model_for("fast"), instruction=prompts.REPORTER,
    tools=[tools.notify_customer], output_key="final_report")

fulfillment_pipeline = SequentialAgent(
    name="fulfillment_pipeline",
    description="End-to-end fulfillment for a new order: intake, parallel recon, plan/validate/heal loop, report.",
    sub_agents=[
        intake_agent,
        ParallelAgent(name="parallel_recon", sub_agents=[inventory_agent, risk_agent]),
        LoopAgent(name="plan_validate_heal_loop", sub_agents=[planner_agent, validator_agent],
                  max_iterations=config.MAX_HEAL_ITERATIONS),
        reporter_agent,
    ])

root_agent = LlmAgent(
    name="shipmind_concierge", model=model_for("balanced"), instruction=prompts.CONCIERGE,
    description="Conversational logistics control tower.",
    tools=data_tools(["track_shipment", "check_inventory", "get_disruptions", "list_warehouses"]),
    sub_agents=[fulfillment_pipeline])
