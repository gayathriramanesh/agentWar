"""ADK function tools. They wrap core.logistics and keep the shared session state up to date.

State keys: order, complexity, auctions, plan, plan_valid, dispatch, heal_log, heal_summary.
"""
from __future__ import annotations

from google.adk.tools.tool_context import ToolContext

from core import logistics
from core.healing import log_event


def _heal(ctx: ToolContext) -> list:
    return list(ctx.state.get("heal_log") or [])


def _save_heal(ctx: ToolContext, log: list) -> None:
    ctx.state["heal_log"] = log
    ctx.state["heal_summary"] = "; ".join(f"{e['kind']}: {e['detail']}" for e in log[-12:]) or "none"


def save_order(order_id: str, sku: str, quantity: int, destination_city: str, priority: str,
               tool_context: ToolContext) -> dict:
    """Validate and store the order. Returns the canonical order with its complexity (low|medium|high).

    Args:
        order_id: Order id such as ORD-1001.
        sku: One of SKU-ECHO, SKU-KINDLE, SKU-LAPTOP, SKU-TV65.
        quantity: Units to ship.
        destination_city: Destination city name.
        priority: express | standard | economy.
    """
    order = logistics.normalize_order(order_id, sku, quantity, destination_city, priority)
    if "error" in order:
        return order
    tool_context.state.update({"order": order, "complexity": order["complexity"], "auctions": {},
                               "plan": {}, "plan_valid": False, "dispatch": {}})
    _save_heal(tool_context, [])
    return order


def run_carrier_auction(origin_warehouse: str, quantity: int, tool_context: ToolContext) -> dict:
    """Get live quotes from all carrier APIs in parallel for one leg and rank them (best = SLA-safe winner).

    Args:
        origin_warehouse: Warehouse id, e.g. SEA1.
        quantity: Units shipped on this leg.
    """
    order = tool_context.state.get("order")
    if not order:
        return {"error": "no order in state; intake must call save_order first"}
    log = _heal(tool_context)
    result = logistics.carrier_auction(origin_warehouse, order["destination_city"],
                                       int(quantity) * order["unit_weight_kg"], order["priority"],
                                       order_id=order["order_id"], heal_log=log)
    auctions = dict(tool_context.state.get("auctions") or {})
    auctions[origin_warehouse] = result
    tool_context.state["auctions"] = auctions
    _save_heal(tool_context, log)
    slim = lambda q: q and {k: q[k] for k in ("carrier", "price_usd", "eta_days", "sla_ok", "score")}  # noqa: E731
    return {"origin": origin_warehouse, "best": slim(result["best"]), "cheapest": slim(result["cheapest"]),
            "ranked": [slim(q) for q in result["ranked"]], "unavailable": result["unavailable"],
            "failed_apis": [f["carrier"] for f in result["failed"]], "elapsed_ms": result["elapsed_ms"]}


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
    if not (len(origins) == len(quantities) == len(carriers)):
        return {"error": "origins, quantities and carriers must have the same length"}
    plan = logistics.build_plan(order, origins, quantities, carriers, tool_context.state.get("auctions") or {})
    tool_context.state.update({"plan": plan, "plan_valid": False})
    return plan


def validate_plan(tool_context: ToolContext) -> dict:
    """Check the submitted plan: stock, quantities, live quotes, SLA. Returns issues with fix suggestions."""
    order, plan = tool_context.state.get("order"), tool_context.state.get("plan")
    if not order or not plan:
        return {"valid": False, "issues": [{"code": "no_plan", "healable": True, "message": "no plan submitted",
                                            "suggestion": "planner must call submit_plan"}]}
    result = logistics.validate_plan(plan, order, tool_context.state.get("auctions") or {})
    tool_context.state["plan_valid"] = result["valid"]
    if not result["valid"]:
        log = _heal(tool_context)
        for i in result["issues"]:
            log_event(log, "plan_rejected", f"{i['code']}: {i['message']} -> {i['suggestion']}")
        _save_heal(tool_context, log)
    return result


def dispatch_shipment(tool_context: ToolContext) -> dict:
    """Book every leg with the carriers (auto fail-over to the next SLA-safe carrier). Only after validation."""
    if not tool_context.state.get("plan_valid"):
        return {"error": "plan not validated; call validate_plan first"}
    log = _heal(tool_context)
    result = logistics.dispatch(tool_context.state["plan"], tool_context.state["order"],
                                tool_context.state.get("auctions") or {}, heal_log=log)
    tool_context.state["dispatch"] = result
    _save_heal(tool_context, log)
    return result


def exit_loop(reason: str, tool_context: ToolContext) -> dict:
    """End the plan -> validate -> heal loop (after a successful dispatch, or to escalate).

    Args:
        reason: Why the loop ends, e.g. "dispatched" or "ESCALATED: insufficient stock".
    """
    tool_context.actions.escalate = True
    tool_context.state["loop_exit_reason"] = reason
    return {"status": "loop_exited", "reason": reason}


def notify_customer(order_id: str, message: str, tool_context: ToolContext) -> dict:
    """Send the customer a notification (mock SMS/email gateway).

    Args:
        order_id: The order id.
        message: Customer-facing message.
    """
    tool_context.state["customer_notification"] = {"order_id": order_id, "message": message}
    return {"status": "sent", "channel": "email+sms (mock)", "order_id": order_id}


def heal_tracker(tool, args, tool_context, tool_response):
    """after_tool_callback: record every tool error so the reporter can explain what was healed."""
    if isinstance(tool_response, dict) and tool_response.get("error"):
        log = _heal(tool_context)
        log_event(log, "tool_error", f"{getattr(tool, 'name', tool)}: {tool_response['error']}")
        _save_heal(tool_context, log)
    return None
