"""Deterministic (non-LLM) agents and gate callbacks.

Inventory lookup, risk lookup and plan validation/dispatch are pure policy: running them through an LLM
added ~8 sequential model round-trips per order and made booking depend on the model remembering to call
`dispatch` and `exit_loop`. Here they are code, so they're instant, exact and can't be talked out of a check.
LLMs keep the parts that need language or judgment: concierge, intake, planner, reporter.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncGenerator, Literal

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, Field

from core import config, data, guards, logistics

from . import tools


def _event(agent: BaseAgent, ctx: InvocationContext, text: str, state_delta: dict, escalate: bool = False) -> Event:
    return Event(author=agent.name, invocation_id=ctx.invocation_id, branch=ctx.branch,
                 content=types.Content(role="model", parts=[types.Part(text=text)]),
                 actions=EventActions(state_delta=state_delta, escalate=escalate or None))


class GuardrailVerdict(BaseModel):
    """Structured output of the input guardrail agent."""

    verdict: Literal["allow", "clarify", "block"]
    risk: Literal["low", "medium", "high"]
    anomalies: list[str] = Field(default_factory=list)
    reason: str


class InventoryAgent(BaseAgent):
    """Allocates stock for the order (nearest site, else split) and records it in state."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        o = ctx.session.state["order"]
        alloc = logistics.allocate_inventory(o["sku"], o["quantity"], o["destination_city"])
        report = f"Allocation status={alloc.get('status')}: " + ", ".join(
            f"{a['warehouse']} x{a['quantity']} ({a['distance_km']} km)" for a in alloc.get("allocations", []))
        yield _event(self, ctx, report, {"allocation": alloc, "inventory_report": report})


class RiskAgent(BaseAgent):
    """Summarises live disruptions and flags any that hit the order's candidate origins."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        o = ctx.session.state["order"]
        disruptions = logistics.get_disruptions()["disruptions"]
        stocked = {wh for wh, n in data.INVENTORY[o["sku"]].items() if n > 0}
        hit = sorted(set(disruptions) & stocked)
        report = ("Active disruptions: " + "; ".join(
            f"{wh} {d['type']} (+{d['delay_days']}d, {d['severity']})" for wh, d in disruptions.items())
            if disruptions else "No active disruptions.")
        if hit:
            report += f" | affects stocked origins: {', '.join(hit)}"
        yield _event(self, ctx, report, {"risk_report": report})


class ValidatorAgent(BaseAgent):
    """Guardrail + executor: validate -> dispatch -> exit, or structured feedback for the next iteration."""

    max_iterations: int = 3

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        log = list(state.get("heal_log") or [])
        auctions = {}
        for origin, auction in (state.get("auctions") or {}).items():
            log.extend(auction.get("heal_events") or [])  # fold in events from the planner's parallel auctions
            auctions[origin] = {**auction, "heal_events": []}
        iteration = int(state.get("heal_iteration") or 0) + 1
        previous = (state.get("validation") or {}).get("issues") if iteration > 1 else None
        review = await asyncio.to_thread(
            logistics.review_plan, state.get("plan") or {}, state["order"], auctions, iteration=iteration,
            max_iterations=self.max_iterations, previous_issues=previous, heal_log=log)

        text = ("APPROVED: " if review["decision"] == "dispatched" else "") + review["reason"]
        delta = {
            "heal_iteration": iteration, "auctions": auctions, "feedback": text, "heal_log": log,
            "heal_summary": "; ".join(f"{e['kind']}: {e['detail']}" for e in log[-12:]) or "none",
            "validation": {"decision": review["decision"], "valid": review["validation"]["valid"],
                           "issues": review["issues"]},
        }
        if review["dispatch"]:
            delta["dispatch"] = review["dispatch"]
        if review["terminal"]:
            delta["outcome"] = {"status": review["decision"].upper(), "reason": review["reason"]}
            delta["loop_exit_reason"] = review["reason"]
        yield _event(self, ctx, text, delta, escalate=review["terminal"])


# --------------------------------------------------------------------------- gate callbacks
def _content(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def screen_input(callback_context):
    """before_agent_callback (concierge): refuse injection / oversized / obfuscated input before any LLM call."""
    content = callback_context.user_content
    text = " ".join(p.text for p in (content.parts or []) if getattr(p, "text", None)) if content else ""
    if issues := guards.screen_user_text(text):
        reason = "REJECTED: input screen (" + "; ".join(issues) + ")"
        callback_context.state["outcome"] = {"status": "REJECTED", "reason": reason}
        return _content(f"I can't process that request ({'; '.join(issues)}). "
                        "Please send a plain order: product, quantity, destination city and delivery speed.")
    return None


def start_order(callback_context):
    """before_agent_callback (fulfillment pipeline): wipe every per-order key so nothing leaks from a
    previous order, then screen the input deterministically before intake and the guardrail agent run."""
    tools.reset_order_state(callback_context.state)
    return screen_input(callback_context)


def require_valid_order(callback_context):
    """before_agent_callback (execution): proceed only if intake produced a grounded order AND the
    guardrail agent allowed it. Two independent checks (code + model) must both pass."""
    state = callback_context.state
    verdict = state.get("guardrail_verdict")
    if state.get("order") and verdict and verdict.get("verdict") == "allow":
        return None
    if state.get("order") and not verdict and config.GUARDRAIL_FAIL_OPEN:
        return None
    if not state.get("order"):
        rejection = state.get("order_rejection") or {"error": "no order could be extracted"}
        detail = rejection.get("ungrounded") or rejection.get("reasons") or rejection.get("error")
    elif not verdict:
        detail = "input guardrail unavailable (failing closed)"
    else:
        detail = f"guardrail {verdict.get('verdict')}: {verdict.get('reason')} {verdict.get('anomalies') or ''}"
    reason = f"REJECTED: order not processed ({detail})"
    state["outcome"] = {"status": "REJECTED", "reason": reason}
    return _content(reason)


# --------------------------------------------------------------------------- execution optimisation
_ORDER_INTENT = re.compile(r"\b(?:ship|send|deliver|order|need|dispatch|want)\b", re.I)


def fast_route(callback_context, llm_request):
    """before_model_callback (concierge): an obvious new order goes straight to the pipeline, skipping
    a routing-only LLM call. Anything else (tracking, questions, ambiguity) still goes to the model."""
    if not config.FAST_ROUTE or not llm_request.contents:
        return None
    last = llm_request.contents[-1]
    if last.role != "user" or any(p.function_response for p in (last.parts or [])):
        return None
    text = " ".join(p.text for p in (last.parts or []) if getattr(p, "text", None))
    if guards.mentioned_skus(text) and _ORDER_INTENT.search(text) and not re.search(r"SM-[A-Z]{3}-", text):
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            name="transfer_to_agent", args={"agent_name": "fulfillment_pipeline"}))]))
    return None


async def fast_path_plan(callback_context):
    """before_agent_callback (planner): routine orders are planned by the rules engine (best SLA-safe
    carrier per allocation leg) with zero LLM calls. The LLM planner takes over for high-complexity
    orders and for every re-plan after validator feedback, where judgment is actually needed."""
    state = callback_context.state
    order, alloc = state.get("order"), state.get("allocation") or {}
    if (not order or (state.get("validation") or {}).get("issues") or not alloc.get("allocations")
            or not config.fast_path_allowed(order["complexity"])):
        return None
    legs = alloc["allocations"]
    results = await asyncio.gather(*(asyncio.to_thread(
        _auction, leg["warehouse"], leg["quantity"], order) for leg in legs))
    auctions = {r["origin"]: r for r in results if "error" not in r}
    carriers = []
    for leg in legs:
        auc = auctions.get(leg["warehouse"]) or {}
        pick = auc.get("best") or auc.get("cheapest")
        if not pick:
            return None  # nothing quotable: let the LLM planner handle it
        carriers.append(pick["carrier"])
    plan = logistics.build_plan(order, [leg["warehouse"] for leg in legs], [leg["quantity"] for leg in legs],
                                carriers, auctions)
    summary = ("Plan (rules fast path): " + ", ".join(f"{leg['origin']}->{leg['carrier']} x{leg['quantity']}"
                                                       for leg in plan["legs"])
               + f" | total ${plan['total_cost_usd']} | max eta {plan['max_eta_days']}d")
    state.update({"auctions": {**(state.get("auctions") or {}), **auctions}, "plan": plan,
                  "plan_summary": summary, "planner_model_route": "fast-path (rules engine, 0 LLM calls)"})
    return _content(summary)


def _auction(origin: str, qty: int, order: dict) -> dict:
    events: list = []
    r = logistics.carrier_auction(origin, order["destination_city"], qty * order["unit_weight_kg"],
                                  order["priority"], order_id=order["order_id"], heal_log=events)
    return r if "error" in r else {**r, "heal_events": events}
