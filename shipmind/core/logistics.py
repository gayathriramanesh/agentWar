"""ShipMind core engine. One implementation shared by the MCP server, the ADK tools and the simulator.

Sections: data tools (exposed via MCP) -> orders -> carrier auction -> plan validation -> dispatch.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import data, store
from .carrier_apis import CarrierAPIError
from .carrier_apis import book as api_book
from .carrier_apis import quote as api_quote
from .healing import BREAKER, log_event, retry

_INV_LOCK = threading.Lock()


def _city(name: str) -> str | None:
    return next((c for c in data.CITIES if c.lower() == str(name).strip().lower()), None)


def _sku(name: str) -> str | None:
    key = str(name).strip().upper()
    if not key.startswith("SKU-"):
        key = "SKU-" + key
    return key if key in data.INVENTORY else None


# --------------------------------------------------------------------------- data tools (MCP)
def list_warehouses() -> dict:
    """List all fulfillment centers with their city and any active disruption."""
    return {"warehouses": [
        {"id": wh, "city": w["city"], "disruption": data.DISRUPTIONS.get(wh)} for wh, w in data.WAREHOUSES.items()
    ]}


def check_inventory(sku: str) -> dict:
    """Units on hand for a SKU in every warehouse. Valid SKUs: SKU-ECHO, SKU-KINDLE, SKU-LAPTOP, SKU-TV65."""
    s = _sku(sku)
    if not s:
        return {"error": f"unknown sku '{sku}'", "valid_skus": list(data.INVENTORY)}
    stock = data.INVENTORY[s]
    return {"sku": s, "by_warehouse": dict(stock), "total_units": sum(stock.values())}


def get_disruptions() -> dict:
    """Active disruptions (weather, labor, outages) per warehouse with the delay they add in days."""
    return {"disruptions": data.DISRUPTIONS, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def allocate_inventory(sku: str, quantity: int, destination_city: str, avoid_disrupted: bool = False) -> dict:
    """Pick the warehouse(s) to ship from: nearest single site with full stock, else a split shipment.

    Set avoid_disrupted=true to deprioritise warehouses with a high-severity disruption.
    """
    s, city = _sku(sku), _city(destination_city)
    if not s or not city:
        return {"error": "invalid sku or city", "valid_skus": list(data.INVENTORY), "valid_cities": list(data.CITIES)}
    quantity = int(quantity)

    def disrupted(wh: str) -> bool:
        return data.DISRUPTIONS.get(wh, {}).get("severity") == "high"

    cands = [
        {"warehouse": wh, "stock": n, "distance_km": data.distance_km(wh, city), "disruption": data.DISRUPTIONS.get(wh)}
        for wh, n in data.INVENTORY[s].items() if n > 0
    ]
    cands.sort(key=lambda c: ((avoid_disrupted and disrupted(c["warehouse"])), c["distance_km"]))

    full = next((c for c in cands if c["stock"] >= quantity), None)
    if full:
        allocs = [{**full, "quantity": quantity}]
    else:
        allocs, left = [], quantity
        for c in cands:
            if left <= 0:
                break
            take = min(left, c["stock"])
            allocs.append({**c, "quantity": take})
            left -= take
    allocated = sum(a["quantity"] for a in allocs)
    return {
        "sku": s, "destination_city": city, "requested": quantity, "allocated": allocated,
        "status": "ok" if allocated >= quantity else "backorder",
        "shortfall": max(0, quantity - allocated), "split": len(allocs) > 1,
        "allocations": [{k: a[k] for k in ("warehouse", "quantity", "distance_km", "disruption")} for a in allocs],
    }


def track_shipment(tracking_id: str) -> dict:
    """Status, carrier, origin, ETA for a tracking id like SM-FED-1A2B3C4D."""
    rec = store.get_shipment(tracking_id.strip())
    if not rec:
        return {"error": f"tracking id '{tracking_id}' not found"}
    return {**rec, "status": "IN_TRANSIT", "last_scan": f"Departed {rec['origin']} facility"}


DATA_TOOLS = [list_warehouses, check_inventory, get_disruptions, allocate_inventory, track_shipment]


# --------------------------------------------------------------------------- orders
def assess_complexity(sku: str, quantity: int, city: str, priority: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    score = 0
    if priority == "express":
        score, reasons = score + 1, reasons + ["express SLA"]
    alloc = allocate_inventory(sku, quantity, city)
    if alloc.get("split"):
        score, reasons = score + 2, reasons + ["split shipment"]
    if alloc.get("status") == "backorder":
        score, reasons = score + 2, reasons + ["insufficient network stock"]
    if any(a["disruption"] for a in alloc.get("allocations", [])):
        score, reasons = score + 1, reasons + ["disrupted origin"]
    if quantity * data.SKU_WEIGHT_KG[sku] > 30:
        score, reasons = score + 1, reasons + ["heavy freight"]
    level = "high" if score >= 3 else "medium" if score >= 1 else "low"
    return level, reasons


def normalize_order(order_id: str, sku: str, quantity: int, destination_city: str, priority: str) -> dict:
    """Validate + canonicalise. Errors list valid options so an LLM can self-correct its tool call."""
    s, city, pr = _sku(sku), _city(destination_city), str(priority).strip().lower()
    errors = {}
    if not s:
        errors["sku"] = list(data.INVENTORY)
    if not city:
        errors["destination_city"] = list(data.CITIES)
    if pr not in data.PRIORITY:
        errors["priority"] = list(data.PRIORITY)
    try:
        qty = int(quantity)
        if qty <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["quantity"] = "positive integer"
        qty = 0
    if errors:
        return {"error": "invalid order fields; retry with valid values", "valid_options": errors}
    level, reasons = assess_complexity(s, qty, city, pr)
    return {
        "order_id": str(order_id).strip() or f"ORD-{int(time.time()) % 10000:04d}",
        "sku": s, "quantity": qty, "destination_city": city, "priority": pr,
        "sla_days": data.PRIORITY[pr]["sla_days"], "unit_weight_kg": data.SKU_WEIGHT_KG[s],
        "complexity": level, "complexity_reasons": reasons,
    }


# --------------------------------------------------------------------------- carrier auction
def score_quotes(quotes: list[dict], priority: str) -> list[dict]:
    """Rank available quotes. Lower score wins; SLA misses get a +1 penalty so they sink."""
    avail = [dict(q) for q in quotes if q.get("available")]
    if not avail:
        return []
    w, sla = data.SCORE_WEIGHTS[priority], data.PRIORITY[priority]["sla_days"]
    prices, etas = [q["price_usd"] for q in avail], [q["eta_days"] for q in avail]

    def norm(v, lo, hi):
        return 0.0 if hi == lo else (v - lo) / (hi - lo)

    for q in avail:
        q["sla_ok"] = q["eta_days"] <= sla
        q["score"] = round(
            w["price"] * norm(q["price_usd"], min(prices), max(prices))
            + w["eta"] * norm(q["eta_days"], min(etas), max(etas))
            + w["risk"] * (1 - q["reliability"]) / 0.1
            + (0 if q["sla_ok"] else 1.0), 4)
    return sorted(avail, key=lambda q: q["score"])


def carrier_auction(origin: str, destination_city: str, weight_kg: float, priority: str,
                    order_id: str = "adhoc", heal_log: list | None = None) -> dict:
    """Call every carrier's rate API in parallel (retry + circuit breaker each) and pick the best."""
    t0 = time.perf_counter()
    city = _city(destination_city) or destination_city

    def one(carrier: str):
        if not BREAKER.allow(carrier):
            log_event(heal_log, "circuit_open", f"skipped {carrier} (circuit open)")
            return {"carrier": carrier, "failed": "circuit open"}
        try:
            q = retry(lambda a: api_quote(carrier, origin, city, weight_kg, priority, key=f"{order_id}:{origin}:{a}"),
                      attempts=3, label=f"{carrier}.rates", heal_log=heal_log, retry_on=(CarrierAPIError,))
            BREAKER.success(carrier)
            return q
        except CarrierAPIError as e:
            BREAKER.failure(carrier)
            log_event(heal_log, "carrier_unreachable", f"{carrier} rates failed after retries: {e}")
            return {"carrier": carrier, "failed": str(e)}

    with ThreadPoolExecutor(max_workers=len(data.CARRIERS)) as ex:
        results = list(ex.map(one, data.CARRIERS))
    ranked = score_quotes([r for r in results if "failed" not in r], priority)
    sla_ok = [q for q in ranked if q["sla_ok"]]
    return {
        "origin": origin, "destination": city, "priority": priority,
        "sla_days": data.PRIORITY[priority]["sla_days"],
        "best": sla_ok[0] if sla_ok else None,
        "cheapest": min(ranked, key=lambda q: q["price_usd"]) if ranked else None,
        "ranked": ranked,
        "unavailable": [{"carrier": r["carrier"], "reason": r["reason"]} for r in results if r.get("available") is False],
        "failed": [r for r in results if "failed" in r],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# --------------------------------------------------------------------------- planning
def build_plan(order: dict, origins: list[str], quantities: list[int], carriers: list[str], auctions: dict) -> dict:
    legs = []
    for o, q, c in zip(origins, quantities, carriers):
        quote = next((x for x in (auctions.get(o) or {}).get("ranked", []) if x["carrier"] == c), None)
        legs.append({"origin": o, "quantity": int(q), "carrier": c, "quote_found": quote is not None,
                     "eta_days": quote and quote["eta_days"], "price_usd": quote and quote["price_usd"],
                     "sla_ok": bool(quote and quote["sla_ok"])})
    return {"order_id": order["order_id"], "legs": legs,
            "total_cost_usd": round(sum(leg["price_usd"] or 0 for leg in legs), 2),
            "max_eta_days": max((leg["eta_days"] or 0 for leg in legs), default=None)}


def validate_plan(plan: dict, order: dict, auctions: dict) -> dict:
    """Guardrail. Returns issues with concrete suggestions so the planner can heal the plan."""
    issues = []
    legs = plan.get("legs", [])
    total = sum(leg["quantity"] for leg in legs)
    network = sum(data.INVENTORY[order["sku"]].values())
    if network < order["quantity"]:
        issues.append({"code": "insufficient_stock", "healable": False,
                       "message": f"network has {network} units, order needs {order['quantity']}",
                       "suggestion": "escalate: create replenishment PO and backorder"})
    elif total != order["quantity"]:
        issues.append({"code": "quantity_mismatch", "healable": True,
                       "message": f"plan ships {total} units, order needs {order['quantity']}",
                       "suggestion": "call allocate_inventory again and plan one leg per allocation"})
    for leg in legs:
        o, c = leg["origin"], leg["carrier"]
        auc = auctions.get(o) or {}
        best = auc.get("best")
        if data.INVENTORY[order["sku"]].get(o, 0) < leg["quantity"]:
            issues.append({"code": "stock", "healable": True, "leg": o,
                           "message": f"{o} only has {data.INVENTORY[order['sku']].get(o, 0)} units",
                           "suggestion": "reallocate inventory"})
        elif not leg["quote_found"]:
            issues.append({"code": "carrier_unavailable", "healable": True, "leg": o,
                           "message": f"no live quote from {c} at {o} (API failure, overweight, or no auction run)",
                           "suggestion": f"use {best['carrier']}" if best else f"run_carrier_auction for {o}"})
        elif not leg["sla_ok"]:
            if best:
                sugg = f"use {best['carrier']} (eta {best['eta_days']}d, ${best['price_usd']})"
                code = "sla_breach"
            else:
                dis = data.DISRUPTIONS.get(o)
                safe_stock = sum(n for wh, n in data.INVENTORY[order["sku"]].items()
                                 if data.DISRUPTIONS.get(wh, {}).get("severity") != "high")
                if safe_stock >= order["quantity"]:
                    sugg = "reallocate with avoid_disrupted=true" + (f" ({o} has {dis['type']})" if dis else "")
                    code = "sla_breach_origin"
                else:  # no site can meet the SLA: escalate now instead of burning loop iterations
                    sugg = "escalate: offer customer a delayed date or a priority downgrade"
                    code = "sla_infeasible"
            issues.append({"code": code, "healable": code != "sla_infeasible", "leg": o,
                           "message": f"{c} eta {leg['eta_days']}d > SLA {order['sla_days']}d", "suggestion": sugg})
    return {"valid": not issues, "issues": issues, "healable": all(i["healable"] for i in issues)}


# --------------------------------------------------------------------------- dispatch
def dispatch(plan: dict, order: dict, auctions: dict, heal_log: list | None = None) -> dict:
    """Book every leg. If a carrier's booking API fails, fail over to the next SLA-compliant carrier."""
    shipments, failed = [], []
    for leg in plan["legs"]:
        ranked = (auctions.get(leg["origin"]) or {}).get("ranked", [])
        primary = next((q for q in ranked if q["carrier"] == leg["carrier"]), None)
        options = ([primary] if primary else []) + [q for q in ranked if q["sla_ok"] and q is not primary]
        booked = None
        for q in options:
            c = q["carrier"]
            if not BREAKER.allow(c):
                log_event(heal_log, "circuit_open", f"booking skipped {c} (circuit open)")
                continue
            try:
                booked = retry(lambda a, c=c, q=q: api_book(c, q, order["order_id"], key=f"{leg['origin']}:{c}:{a}"),
                               attempts=2, label=f"{c}.book", heal_log=heal_log, retry_on=(CarrierAPIError,))
                BREAKER.success(c)
                if c != leg["carrier"]:
                    log_event(heal_log, "carrier_failover", f"{leg['origin']}: {leg['carrier']} -> {c}")
                break
            except CarrierAPIError as e:
                BREAKER.failure(c)
                log_event(heal_log, "booking_failed", f"{c} booking failed: {e}")
        if not booked:
            failed.append({"origin": leg["origin"], "carrier": leg["carrier"]})
            continue
        with _INV_LOCK:
            data.INVENTORY[order["sku"]][leg["origin"]] -= leg["quantity"]
        rec = {**booked, "order_id": order["order_id"], "sku": order["sku"], "quantity": leg["quantity"],
               "origin": leg["origin"], "destination": order["destination_city"]}
        store.save_shipment(rec)
        shipments.append(rec)
    status = "DISPATCHED" if not failed else ("PARTIAL" if shipments else "FAILED")
    return {"status": status, "shipments": shipments, "failed_legs": failed,
            "total_cost_usd": round(sum(s["price_usd"] for s in shipments), 2),
            "max_eta_days": max((s["eta_days"] for s in shipments), default=None)}
