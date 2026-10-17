"""Token economics: per-order token accounting, cost, and a hard budget.

- record_usage (after_model_callback on every LLM agent) accumulates input/output/cached tokens and USD
  cost per agent. One state key per agent, because parallel agents writing one shared dict would
  overwrite each other's counts.
- enforce_budget (before_model_callback) refuses further model calls once an order exceeds
  SHIPMIND_TOKEN_BUDGET_PER_ORDER; the pipeline then escalates instead of burning money in a loop.
- over_downgrade_threshold lets the planner router drop the deep model when an order is getting expensive.
Accounting is scoped to the invocation (one user message = one order), so it never leaks across orders.
"""
from __future__ import annotations

import json
import time

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from core import config

LLM_AGENTS = ("shipmind_concierge", "intake_agent", "input_guardrail_agent", "planner_agent", "reporter_agent")


def _key(agent_name: str) -> str:
    return f"tokens_{agent_name}"


def _current(state, agent_name: str, invocation_id: str) -> dict:
    entry = state.get(_key(agent_name)) or {}
    if entry.get("invocation_id") != invocation_id:
        entry = {"invocation_id": invocation_id, "calls": 0, "input": 0, "output": 0, "cached": 0,
                 "cost_usd": 0.0, "models": {}}
    return dict(entry)


def order_usage(state, invocation_id: str | None = None) -> dict:
    """Aggregate usage for one order (the given invocation, or the latest one seen)."""
    entries = {a: state.get(_key(a)) for a in LLM_AGENTS if state.get(_key(a))}
    if invocation_id is None and entries:
        invocation_id = max(entries.values(), key=lambda e: e.get("last_ts", 0)).get("invocation_id")
    per_agent = {a: e for a, e in entries.items() if e.get("invocation_id") == invocation_id}
    total = {k: sum(e[k] for e in per_agent.values()) for k in ("calls", "input", "output", "cached")}
    return {**total, "total_tokens": total["input"] + total["output"],
            "cost_usd": round(sum(e["cost_usd"] for e in per_agent.values()), 6),
            "budget": config.TOKEN_BUDGET_PER_ORDER,
            "per_agent": {a: {k: e[k] for k in ("calls", "input", "output", "cached", "cost_usd")}
                          for a, e in per_agent.items()}}


def record_usage(callback_context, llm_response: LlmResponse):
    """after_model_callback: account tokens and cost for this call."""
    um = llm_response.usage_metadata
    if um is None:
        return None
    entry = _current(callback_context.state, callback_context.agent_name, callback_context.invocation_id)
    inp, out = um.prompt_token_count or 0, (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
    cached = um.cached_content_token_count or 0
    price_in, price_out = config.price_for_model(llm_response.model_version or "")
    cost = ((inp - cached) * price_in + cached * price_in * config.CACHED_INPUT_DISCOUNT + out * price_out) / 1e6
    model = llm_response.model_version or "unknown"
    entry.update(calls=entry["calls"] + 1, input=entry["input"] + inp, output=entry["output"] + out,
                 cached=entry["cached"] + cached, cost_usd=round(entry["cost_usd"] + cost, 6),
                 models={**entry["models"], model: entry["models"].get(model, 0) + 1},
                 last_ts=time.time())
    callback_context.state[_key(callback_context.agent_name)] = entry
    return None


def used_tokens(callback_context) -> int:
    return order_usage(callback_context.state, callback_context.invocation_id)["total_tokens"]


def over_downgrade_threshold(callback_context) -> bool:
    return used_tokens(callback_context) >= config.BUDGET_DOWNGRADE_AT * config.TOKEN_BUDGET_PER_ORDER


def enforce_budget(callback_context, llm_request):
    """before_model_callback: refuse the call (no tokens spent) when the order is over budget."""
    used = used_tokens(callback_context)
    if used < config.TOKEN_BUDGET_PER_ORDER:
        return None
    log = list(callback_context.state.get("heal_log") or [])
    log.append({"ts": round(time.time(), 3), "kind": "token_budget",
                "detail": f"{callback_context.agent_name} blocked: {used} >= {config.TOKEN_BUDGET_PER_ORDER} tokens"})
    callback_context.state["heal_log"] = log
    reason = f"TOKEN_BUDGET_EXCEEDED: order used {used} tokens (budget {config.TOKEN_BUDGET_PER_ORDER})"
    if llm_request.config and llm_request.config.response_schema:
        # Structured-output agents (the guardrail) must still get schema-valid JSON: fail closed.
        text = json.dumps({"verdict": "block", "risk": "high", "anomalies": ["token budget exceeded"],
                           "reason": reason})
    else:
        text = reason
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
