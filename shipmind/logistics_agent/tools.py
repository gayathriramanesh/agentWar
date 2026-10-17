"""ADK function tools. They wrap core.logistics and keep the shared session state up to date.

State keys: see ORDER_STATE (all reset at the start of every order so nothing leaks between orders).
Blocking engine calls (carrier APIs) run in a worker thread: a sync tool would otherwise block ADK's event
loop, which serialises the planner's parallel auctions and stalls every other session on the server.
"""
from __future__ import annotations

import asyncio

from google.adk.tools.tool_context import ToolContext

from core import claims, guards, logistics
from core.healing import log_event

from .prompts import user_messages

MAX_INTAKE_ATTEMPTS = 3

# Every per-order state key and its empty value. Reset at the start of each pipeline run.
ORDER_STATE = {
    "order": None, "complexity": None, "order_rejection": None, "intake_attempts": 0,
    "allocation": None, "inventory_report": None, "risk_report": None,
    "auctions": {}, "plan": {}, "plan_summary": None, "planner_model_route": None,
    "validation": None, "feedback": None, "heal_iteration": 0,
    "dispatch": {}, "outcome": None, "loop_exit_reason": None,
    "customer_notification": None, "final_report": None, "order_summary": None, "guardrail_verdict": None,
    "budget_downgrade": False, "heal_log": [], "heal_summary": "none",
}


def reset_order_state(state) -> None:
    for key, empty in ORDER_STATE.items():
        state[key] = empty.copy() if isinstance(empty, (dict, list)) else empty


def _heal(ctx: ToolContext) -> list:
    return list(ctx.state.get("heal_log") or [])


def _save_heal(ctx, log: list) -> None:
    ctx.state["heal_log"] = log
    ctx.state["heal_summary"] = "; ".join(f"{e['kind']}: {e['detail']}" for e in log[-12:]) or "none"


def _reject(ctx: ToolContext, result: dict) -> dict:
    ctx.state["order_rejection"] = result
    if not result.get("retry_allowed", True):
        ctx.actions.skip_summarization = True  # final: the execution gate reports it, no extra LLM call
    return result


def save_order(order_id: str, sku: str, quantity: int, destination_city: str, priority: str,
               tool_context: ToolContext) -> dict:
    """Validate and store the order. Returns the canonical order with its complexity (low|medium|high).

    Only pass values the user actually stated. Unknown or missing values must be asked for, not guessed.

    Args:
        order_id: The user's order id such as ORD-1001, or "" if the user gave none (one is generated).
        sku: One of SKU-ECHO, SKU-KINDLE, SKU-LAPTOP, SKU-TV65.
        quantity: Whole number of units the user asked for.
        destination_city: Destination city name exactly as the user gave it.
        priority: express | standard | economy, as the user asked.
    """
    attempts = int(tool_context.state.get("intake_attempts") or 0) + 1
    tool_context.state["intake_attempts"] = attempts
    if attempts > MAX_INTAKE_ATTEMPTS:
        return _reject(tool_context, {"error": f"too many invalid attempts ({MAX_INTAKE_ATTEMPTS})",
                                      "retry_allowed": False})
    order = logistics.normalize_order(order_id, sku, quantity, destination_city, priority)
    if "error" in order:
        return _reject(tool_context, {**order, "retry_allowed": order.get("retry_allowed", True),
                                      "instruction": "Fix formatting only. Never substitute a value the user "
                                                     "did not say; if a value is unknown, ask the user."})
    ungrounded = guards.grounding_issues(order, "\n".join(user_messages(tool_context)))
    if ungrounded:
        return _reject(tool_context, {"error": "order fields are not supported by the user's message",
                                      "ungrounded": ungrounded, "retry_allowed": False,
                                      "instruction": "Do not retry. Ask the user to confirm these fields."})
    summary = (f"{order['order_id']}: {order['sku']} x{order['quantity']} -> {order['destination_city']}, "
               f"{order['priority']}, complexity={order['complexity']}")
    tool_context.state.update({"order": order, "complexity": order["complexity"], "order_rejection": None,
                               "order_summary": summary})
    tool_context.actions.skip_summarization = True  # the tool result is the answer; saves one model call
    return order


def reallocate_inventory(avoid_disrupted: bool, tool_context: ToolContext) -> dict:
    """Re-run inventory allocation for the current order (use when the validator says "reallocate").

    Args:
        avoid_disrupted: True to steer away from warehouses with a high-severity disruption.
    """
    order = tool_context.state.get("order")
    if not order:
        return {"error": "no order in state; intake must call save_order first"}
    alloc = logistics.allocate_inventory(order["sku"], order["quantity"], order["destination_city"],
                                         avoid_disrupted=bool(avoid_disrupted))
    if "error" not in alloc:
        tool_context.state["allocation"] = alloc
    return alloc


async def run_carrier_auction(origin_warehouse: str, quantity: int, tool_context: ToolContext) -> dict:
    """Get live quotes from all carrier APIs in parallel for one leg and rank them (best = SLA-safe winner).

    Args:
        origin_warehouse: Warehouse id, e.g. SEA1.
        quantity: Units shipped on this leg.
    """
    order = tool_context.state.get("order")
    if not order:
        return {"error": "no order in state; intake must call save_order first"}
    qty, err = guards.parse_quantity(quantity)
    if err or qty > order["quantity"]:
        return {"error": err or f"leg quantity {qty} exceeds the order quantity {order['quantity']}"}
    events: list = []
    result = await asyncio.to_thread(
        logistics.carrier_auction, origin_warehouse, order["destination_city"], qty * order["unit_weight_kg"],
        order["priority"], order_id=order["order_id"], heal_log=events)
    if "error" in result:
        return result
    # Heal events ride inside the auction record: parallel tool calls deep-merge dict state, but a shared
    # heal_log list would be overwritten by whichever call finishes last.
    tool_context.state["auctions"] = {**(tool_context.state.get("auctions") or {}),
                                      result["origin"]: {**result, "heal_events": events}}
    slim = lambda q: q and {k: q[k] for k in ("carrier", "price_usd", "eta_days", "sla_ok", "score")}  # noqa: E731
    return {"origin": result["origin"], "quantity": qty, "best": slim(result["best"]),
            "cheapest": slim(result["cheapest"]), "ranked": [slim(q) for q in result["ranked"]],
            "unavailable": result["unavailable"], "failed_apis": [f["carrier"] for f in result["failed"]],
            "elapsed_ms": result["elapsed_ms"]}


def submit_plan(origins: list[str], quantities: list[int], carriers: list[str], tool_context: ToolContext) -> dict:
    """Submit the fulfillment plan as parallel lists (one entry per leg).

    Args:
        origins: Warehouse id per leg, e.g. ["ORD4", "DFW7"].
        quantities: Units per leg, e.g. [4, 4].
        carriers: Carrier per leg, e.g. ["UPS", "FedEx"].
    """
    order = tool_context.state.get("order")
    if not order:
        return {"error": "no order in state"}
    if err := logistics.check_plan_inputs(origins, quantities, carriers):
        return err
    plan = logistics.build_plan(order, origins, quantities, carriers, tool_context.state.get("auctions") or {})
    tool_context.state["plan"] = plan
    return plan


def ops_report(state) -> str:
    """Deterministic ops report: every number comes from engine state, so it cannot be hallucinated."""
    order, d, outcome = state.get("order") or {}, state.get("dispatch") or {}, state.get("outcome") or {}
    legs = "; ".join(f"{x['origin']} -> {x['carrier']} x{x['quantity']}, eta {x['eta_days']}d, ${x['price_usd']} "
                     f"[{x['tracking_id']}]" for x in d.get("shipments", [])) or "none booked"
    failed = "; ".join(f"{f['origin']} ({f['reason']})" for f in d.get("failed_legs", []))
    return (f"OPS REPORT {order.get('order_id')} | status {outcome.get('status')} | {outcome.get('reason')}\n"
            f"legs: {legs}" + (f"\nfailed legs: {failed}" if failed else "")
            + f"\ntotal ${d.get('total_cost_usd', 0)} | heal iterations {state.get('heal_iteration') or 0}"
            + f"\nself-healing: {state.get('heal_summary') or 'none'}")


def notify_customer(message: str, tool_context: ToolContext) -> dict:
    """Send the customer a notification for the current order (mock SMS/email gateway).

    The message is checked against the booked shipments: any tracking id, price, ETA, carrier or
    "shipped" claim that isn't backed by the order's records is refused and must be rewritten.

    Args:
        message: Customer-facing message (max 600 characters).
    """
    order = tool_context.state.get("order") or {}
    if not order:
        return {"error": "no order in state"}
    if tool_context.state.get("customer_notification"):
        return {"status": "already_sent", "order_id": order["order_id"]}  # never message a customer twice
    text = str(message or "").strip()
    if not text or len(text) > 600:
        return {"error": "message must be 1-600 characters"}
    check = claims.check_claims(text, claims.facts_from_state(tool_context.state))
    if check["unsupported"]:
        return {"error": "message contains claims not supported by the order records; rewrite it",
                "unsupported_claims": check["unsupported"]}
    tool_context.state["customer_notification"] = {"order_id": order["order_id"], "message": text}
    tool_context.state["final_report"] = ops_report(tool_context.state)
    tool_context.actions.skip_summarization = True  # report is deterministic; no second model call
    return {"status": "sent", "channel": "email+sms (mock)", "order_id": order["order_id"]}


def heal_tracker(tool, args, tool_context, tool_response):
    """after_tool_callback: record every tool error so the reporter can explain what was healed."""
    if isinstance(tool_response, dict) and tool_response.get("error"):
        log = _heal(tool_context)
        log_event(log, "tool_error", f"{getattr(tool, 'name', tool)}: {tool_response['error']}")
        _save_heal(tool_context, log)
    return None
