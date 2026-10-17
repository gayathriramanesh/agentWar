"""Output grounding: check every factual claim in LLM-written text against the system of record.

Used twice: as a guardrail inside `notify_customer` (an ungrounded customer message is refused and the
model must rewrite it) and as the hallucination / truthfulness metric in the eval harness.
"""
from __future__ import annotations

import re

from . import data

_TRACKING = re.compile(r"\bSM-[A-Z]{3}-[0-9A-F]{8}\b")
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_DAYS = re.compile(r"\b(\d{1,3})\s*-?\s*(?:business\s+|calendar\s+)?(?:days?|d)\b", re.I)
_SHIPPED = re.compile(r"\b(?:on (?:its|the) way|has (?:been )?shipped|is shipped|dispatched|in transit|"
                      r"will (?:arrive|be delivered)|has been booked)\b", re.I)


def facts_from_state(state: dict) -> dict:
    """The ground truth an output may cite, built only from engine-produced state."""
    order = state.get("order") or {}
    dispatch = state.get("dispatch") or {}
    shipments = dispatch.get("shipments") or []
    outcome = (state.get("outcome") or {}).get("status")
    prices = {round(float(s["price_usd"]), 2) for s in shipments}
    if dispatch.get("total_cost_usd") is not None:
        prices.add(round(float(dispatch["total_cost_usd"]), 2))
    days = {int(s["eta_days"]) for s in shipments}
    if order.get("sla_days"):
        days.add(int(order["sla_days"]))
    return {
        "status": outcome, "order_id": order.get("order_id"),
        "tracking_ids": {s["tracking_id"] for s in shipments},
        "prices": prices, "days": days,
        "carriers": {s["carrier"] for s in shipments}, "origins": {s["origin"] for s in shipments},
    }


def check_claims(text: str, facts: dict) -> dict:
    """Returns {"claims": n, "unsupported": [...]} for tracking ids, prices, day counts, carriers,
    warehouses and shipment-status claims made in `text`."""
    text = text or ""
    unsupported, n = [], 0
    for tid in _TRACKING.findall(text):
        n += 1
        if tid not in facts["tracking_ids"]:
            unsupported.append(f"tracking id {tid} does not exist for this order")
    for amount in _MONEY.findall(text):
        n += 1
        value = round(float(amount.replace(",", "")), 2)
        if not any(abs(value - p) < 0.011 for p in facts["prices"]):
            unsupported.append(f"amount ${value} matches no quoted or booked price")
    for days in _DAYS.findall(text):
        n += 1
        if int(days) not in facts["days"]:
            unsupported.append(f"'{days} days' matches no ETA or SLA")
    for carrier in data.CARRIERS:
        if re.search(rf"\b{re.escape(carrier)}\b", text, re.I):
            n += 1
            if carrier not in facts["carriers"]:
                unsupported.append(f"carrier {carrier} was not booked")
    for wh in data.WAREHOUSES:
        if re.search(rf"\b{wh}\b", text):
            n += 1
            if wh not in facts["origins"]:
                unsupported.append(f"warehouse {wh} is not a shipping origin")
    if _SHIPPED.search(text):
        n += 1
        if facts["status"] not in ("DISPATCHED", "PARTIAL"):
            unsupported.append(f"claims the order shipped but the outcome is {facts['status']}")
    return {"claims": n, "unsupported": unsupported}
