"""ShipMind core engine. One implementation shared by the MCP server, the ADK tools and the simulator.

Sections: data tools (exposed via MCP) -> orders -> carrier auction -> plan validation -> dispatch -> review.
Every public function returns {"error": ...} for bad input instead of raising, so LLM callers can recover.
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import config, data, guards, store
from .carrier_apis import CarrierAPIError
from .carrier_apis import book as api_book
from .carrier_apis import quote as api_quote
from .healing import BREAKER, log_event, retry

_INV_LOCK = threading.Lock()
# Short-lived cache of successful rate quotes: heal-loop re-auctions and bursts of similar orders reuse
# them instead of re-calling five carrier APIs. Bookings are never cached.
_QUOTE_CACHE: dict[tuple, tuple[float, dict]] = {}
_QUOTE_LOCK = threading.Lock()


def clear_quote_cache() -> None:
    with _QUOTE_LOCK:
        _QUOTE_CACHE.clear()


def _cached_quote(key: tuple) -> dict | None:
    if config.QUOTE_CACHE_TTL_S <= 0:
        return None
    with _QUOTE_LOCK:
        hit = _QUOTE_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < config.QUOTE_CACHE_TTL_S:
            return dict(hit[1])
        _QUOTE_CACHE.pop(key, None)
        return None


def _store_quote(key: tuple, q: dict) -> None:
    if config.QUOTE_CACHE_TTL_S > 0:
        with _QUOTE_LOCK:
            _QUOTE_CACHE[key] = (time.monotonic(), dict(q))


def _city(name) -> str | None:
    return guards.resolve_city(name)


def _sku(name) -> str | None:
    key = str(name or "").strip().upper()
    if not key:
        return None
    if not key.startswith("SKU-"):
        key = "SKU-" + key
    return key if key in data.INVENTORY else None


def _warehouse(name) -> str | None:
    key = str(name or "").strip().upper()
    return key if key in data.WAREHOUSES else None


def _carrier(name) -> str | None:
    key = str(name or "").strip().lower()
    return next((c for c in data.CARRIERS if c.lower() == key), None)


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
    stock = dict(data.INVENTORY[s])
    return {"sku": s, "by_warehouse": stock, "total_units": sum(stock.values())}


def get_disruptions() -> dict:
    """Active disruptions (weather, labor, outages) per warehouse with the delay they add in days."""
    return {"disruptions": data.DISRUPTIONS, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def allocate_inventory(sku: str, quantity: int, destination_city: str, avoid_disrupted: bool = False) -> dict:
    """Pick the warehouse(s) to ship from: nearest single site with full stock, else a split shipment.

    Set avoid_disrupted=true to deprioritise warehouses with a high-severity disruption.
    """
    s, city = _sku(sku), _city(destination_city)
    qty, qty_error = guards.parse_quantity(quantity)
    if not s or not city or qty_error:
        return {"error": qty_error or "invalid sku or city", "valid_skus": list(data.INVENTORY),
                "valid_cities": list(data.CITIES)}

    def disrupted(wh: str) -> bool:
        return data.DISRUPTIONS.get(wh, {}).get("severity") == "high"

    cands = [
        {"warehouse": wh, "stock": n, "distance_km": data.distance_km(wh, city), "disruption": data.DISRUPTIONS.get(wh)}
        for wh, n in data.INVENTORY[s].items() if n > 0
    ]
    cands.sort(key=lambda c: ((bool(avoid_disrupted) and disrupted(c["warehouse"])), c["distance_km"]))

    full = next((c for c in cands if c["stock"] >= qty), None)
    if full:
        allocs = [{**full, "quantity": qty}]
    else:
        allocs, left = [], qty
        for c in cands:
            if left <= 0:
                break
            take = min(left, c["stock"])
            allocs.append({**c, "quantity": take})
            left -= take
    allocated = sum(a["quantity"] for a in allocs)
    return {
        "sku": s, "destination_city": city, "requested": qty, "allocated": allocated,
        "status": "ok" if allocated >= qty else "backorder",
        "shortfall": max(0, qty - allocated), "split": len(allocs) > 1,
        "allocations": [{k: a[k] for k in ("warehouse", "quantity", "distance_km", "disruption")} for a in allocs],
    }


def track_shipment(tracking_id: str) -> dict:
    """Status, carrier, origin, ETA for a tracking id like SM-FED-1A2B3C4D."""
    tid = str(tracking_id or "").strip().upper()
    rec = store.get_shipment(tid) if tid else None
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
    """Validate + canonicalise. Errors list valid options so an LLM can fix a mis-formatted tool call.

    Anomalies (non-integer / negative / absurd quantities, malformed or duplicate order ids) are rejected,
    never coerced: truncating 2.7 to 2 or re-shipping an order already dispatched is worse than asking.
    """
    s, city, pr = _sku(sku), _city(destination_city), str(priority or "").strip().lower()
    qty, qty_error = guards.parse_quantity(quantity)
    oid, oid_error = guards.clean_order_id(order_id)
    errors, reasons = {}, {}
    if not s:
        errors["sku"], reasons["sku"] = list(data.INVENTORY), f"unknown sku '{sku}'"
    if not city:
        errors["destination_city"] = list(data.CITIES)
        reasons["destination_city"] = f"'{destination_city}' is not a city we deliver to"
    if pr not in data.PRIORITY:
        errors["priority"], reasons["priority"] = list(data.PRIORITY), f"unknown priority '{priority}'"
    if qty_error:
        errors["quantity"], reasons["quantity"] = f"whole number 1..{config.MAX_UNITS_PER_ORDER}", qty_error
    if oid_error:
        errors["order_id"], reasons["order_id"] = "letters, digits, '.', '_' or '-'", oid_error
    if errors:
        return {"error": "invalid order fields", "reasons": reasons, "valid_options": errors}
    if booked := store.find_shipments(oid):
        return {"error": f"duplicate order: {oid} was already dispatched", "retry_allowed": False,
                "tracking_ids": [b["tracking_id"] for b in booked]}
    level, reasons_ = assess_complexity(s, qty, city, pr)
    return {
        "order_id": oid, "sku": s, "quantity": qty, "destination_city": city, "priority": pr,
        "sla_days": data.PRIORITY[pr]["sla_days"], "unit_weight_kg": data.SKU_WEIGHT_KG[s],
        "complexity": level, "complexity_reasons": reasons_,
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


def quote_anomaly(q: dict) -> str | None:
    """Sanity-check a carrier's quote. Upstream APIs do return garbage; never rank or book it."""
    if not q.get("available"):
        return None
    price, eta, rel = q.get("price_usd"), q.get("eta_days"), q.get("reliability")
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return f"non-positive or invalid price {price!r}"
    if not isinstance(eta, int) or isinstance(eta, bool) or not 1 <= eta <= 60:
        return f"implausible eta {eta!r}"
    if not isinstance(rel, (int, float)) or not 0 < rel <= 1:
        return f"invalid reliability {rel!r}"
    return None


def carrier_auction(origin: str, destination_city: str, weight_kg: float, priority: str,
                    order_id: str = "adhoc", heal_log: list | None = None) -> dict:
    """Call every carrier's rate API in parallel (retry + circuit breaker each) and pick the best.

    The whole auction has a deadline (SHIPMIND_AUCTION_TIMEOUT_MS): a slow carrier is dropped, it
    doesn't hold the order hostage.
    """
    t0 = time.perf_counter()
    o, city = _warehouse(origin), _city(destination_city)
    errors = {}
    if not o:
        errors["origin"] = list(data.WAREHOUSES)
    if not city:
        errors["destination_city"] = list(data.CITIES)
    if priority not in data.PRIORITY:
        errors["priority"] = list(data.PRIORITY)
    if not isinstance(weight_kg, (int, float)) or not math.isfinite(weight_kg) or weight_kg <= 0:
        errors["weight_kg"] = "positive number"
    if errors:
        return {"error": "invalid auction request", "valid_options": errors}

    def one(carrier: str):
        events: list = []  # per-call log, merged only if the call finishes before the deadline
        cache_key = (carrier, o, city, round(weight_kg, 3), priority)
        if cached := _cached_quote(cache_key):
            return {**cached, "cached": True}, events
        if not BREAKER.allow(carrier):
            log_event(events, "circuit_open", f"skipped {carrier} (circuit open)")
            return {"carrier": carrier, "failed": "circuit open"}, events
        try:
            q = retry(lambda a: api_quote(carrier, o, city, weight_kg, priority, key=f"{order_id}:{o}:{a}"),
                      attempts=3, label=f"{carrier}.rates", heal_log=events, retry_on=(CarrierAPIError,))
        except CarrierAPIError as e:
            BREAKER.failure(carrier)
            log_event(events, "carrier_unreachable", f"{carrier} rates failed after retries: {e}")
            return {"carrier": carrier, "failed": str(e)}, events
        BREAKER.success(carrier)
        if problem := quote_anomaly(q):
            log_event(events, "quote_anomaly", f"{carrier} quote rejected: {problem}")
            return {"carrier": carrier, "failed": f"anomalous quote: {problem}"}, events
        _store_quote(cache_key, q)
        return q, events

    ex = ThreadPoolExecutor(max_workers=len(data.CARRIERS))
    futures = {c: ex.submit(one, c) for c in data.CARRIERS}
    done, _ = wait(futures.values(), timeout=config.AUCTION_TIMEOUT_MS / 1000)
    ex.shutdown(wait=False, cancel_futures=True)
    results = []
    for carrier, fut in futures.items():
        if fut in done:
            result, events = fut.result()
            if heal_log is not None:
                heal_log.extend(events)
        else:
            BREAKER.failure(carrier)
            log_event(heal_log, "carrier_timeout", f"{carrier} missed the {config.AUCTION_TIMEOUT_MS} ms deadline")
            result = {"carrier": carrier, "failed": "timeout"}
        results.append(result)

    ranked = score_quotes([r for r in results if "failed" not in r], priority)
    sla_ok = [q for q in ranked if q["sla_ok"]]
    return {
        "origin": o, "destination": city, "priority": priority, "weight_kg": round(weight_kg, 3),
        "sla_days": data.PRIORITY[priority]["sla_days"],
        "best": sla_ok[0] if sla_ok else None,
        "cheapest": min(ranked, key=lambda q: q["price_usd"]) if ranked else None,
        "ranked": ranked,
        "unavailable": [{"carrier": r["carrier"], "reason": r["reason"]} for r in results if r.get("available") is False],
        "failed": [r for r in results if "failed" in r],
        "cache_hits": sum(1 for r in results if r.get("cached")),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# --------------------------------------------------------------------------- planning
def check_plan_inputs(origins, quantities, carriers) -> dict | None:
    """Shape/type check for a plan coming from an LLM. Returns an error dict, or None if well-formed."""
    if not all(isinstance(x, list) for x in (origins, quantities, carriers)):
        return {"error": "origins, quantities and carriers must be lists"}
    if not origins or not (len(origins) == len(quantities) == len(carriers)):
        return {"error": "origins, quantities and carriers must be non-empty and the same length"}
    bad = {}
    if unknown := [o for o in origins if not _warehouse(o)]:
        bad["origins"] = {"unknown": unknown, "valid": list(data.WAREHOUSES)}
    if unknown := [c for c in carriers if not _carrier(c)]:
        bad["carriers"] = {"unknown": unknown, "valid": list(data.CARRIERS)}
    if errs := [err for _, err in map(guards.parse_quantity, quantities) if err]:
        bad["quantities"] = errs
    return {"error": "invalid plan", "details": bad} if bad else None


def build_plan(order: dict, origins: list[str], quantities: list[int], carriers: list[str], auctions: dict) -> dict:
    legs = []
    for o_, q_, c_ in zip(origins, quantities, carriers):
        o, c = _warehouse(o_) or str(o_), _carrier(c_) or str(c_)
        q = guards.parse_quantity(q_)[0] or 0
        auc = auctions.get(o) or {}
        quote = next((x for x in auc.get("ranked", []) if x["carrier"] == c), None)
        legs.append({"origin": o, "quantity": q, "carrier": c, "quote_found": quote is not None,
                     "quote_weight_kg": auc.get("weight_kg"),
                     "eta_days": quote and quote["eta_days"], "price_usd": quote and quote["price_usd"],
                     "sla_ok": bool(quote and quote["sla_ok"])})
    return {"order_id": order["order_id"], "legs": legs,
            "total_cost_usd": round(sum(leg["price_usd"] or 0 for leg in legs), 2),
            "max_eta_days": max((leg["eta_days"] or 0 for leg in legs), default=None)}


def _issue(code: str, message: str, suggestion: str, action: dict, leg: str | None = None) -> dict:
    """One validation finding. `action` is machine-readable so the planner never has to parse prose."""
    healable = action["type"] != "escalate"
    return {"code": code, "healable": healable, "message": message, "suggestion": suggestion, "action": action,
            **({"leg": leg} if leg else {})}


def validate_plan(plan: dict, order: dict, auctions: dict) -> dict:
    """Guardrail. Returns issues with concrete, machine-readable fixes so the planner can heal the plan."""
    issues = []
    legs = plan.get("legs", [])
    stock = data.INVENTORY[order["sku"]]
    reallocate = {"type": "reallocate", "avoid_disrupted": False}
    if not legs:
        issues.append(_issue("empty_plan", "plan has no legs", "plan one leg per allocation", reallocate))
    network = sum(stock.values())
    total = sum(leg["quantity"] for leg in legs)
    if network < order["quantity"]:
        issues.append(_issue("insufficient_stock", f"network has {network} units, order needs {order['quantity']}",
                             "escalate: create replenishment PO and backorder", {"type": "escalate"}))
    elif legs and total != order["quantity"]:
        issues.append(_issue("quantity_mismatch", f"plan ships {total} units, order needs {order['quantity']}",
                             "call reallocate_inventory and plan one leg per allocation", reallocate))
    per_origin: dict[str, int] = {}
    for leg in legs:
        per_origin[leg["origin"]] = per_origin.get(leg["origin"], 0) + leg["quantity"]
    for o, n in per_origin.items():
        if sum(1 for leg in legs if leg["origin"] == o) > 1:
            issues.append(_issue("duplicate_origin", f"{o} appears in several legs", "merge them into one leg",
                                 reallocate, leg=o))

    for leg in legs:
        o, c, q = leg["origin"], leg["carrier"], leg["quantity"]
        best = (auctions.get(o) or {}).get("best")
        if o not in data.WAREHOUSES:
            issues.append(_issue("unknown_origin", f"{o} is not a warehouse", "reallocate inventory", reallocate, o))
        elif q <= 0:
            issues.append(_issue("bad_quantity", f"leg quantity {q} must be positive", "reallocate inventory",
                                 reallocate, o))
        elif stock.get(o, 0) < per_origin[o]:
            issues.append(_issue("stock", f"{o} only has {stock.get(o, 0)} units", "reallocate inventory",
                                 reallocate, o))
        elif not leg["quote_found"]:
            issues.append(_issue(
                "carrier_unavailable", f"no live quote from {c} at {o} (API failure, overweight, or no auction run)",
                f"use {best['carrier']}" if best else f"run_carrier_auction for {o}",
                {"type": "use_carrier", "carrier": best["carrier"]} if best
                else {"type": "rerun_auction", "quantity": q}, o))
        elif not math.isclose(leg["quote_weight_kg"] or 0, q * order["unit_weight_kg"], abs_tol=1e-3):
            issues.append(_issue("stale_quote", f"{o} quote was for {leg['quote_weight_kg']} kg, leg ships "
                                 f"{q * order['unit_weight_kg']:.3f} kg", f"run_carrier_auction({o}, {q}) again",
                                 {"type": "rerun_auction", "quantity": q}, o))
        elif not leg["sla_ok"]:
            msg = f"{c} eta {leg['eta_days']}d > SLA {order['sla_days']}d"
            if best:
                issues.append(_issue("sla_breach", msg, f"use {best['carrier']} (eta {best['eta_days']}d, "
                                     f"${best['price_usd']})", {"type": "use_carrier", "carrier": best["carrier"]}, o))
                continue
            dis = data.DISRUPTIONS.get(o)
            safe_stock = sum(n for wh, n in stock.items() if data.DISRUPTIONS.get(wh, {}).get("severity") != "high")
            if dis and dis.get("severity") == "high" and safe_stock >= order["quantity"]:
                issues.append(_issue("sla_breach_origin", msg, f"reallocate with avoid_disrupted=true ({o} has "
                                     f"{dis['type']})", {"type": "reallocate", "avoid_disrupted": True}, o))
            else:  # no site can meet the SLA: escalate now instead of burning loop iterations
                issues.append(_issue("sla_infeasible", msg, "escalate: offer customer a delayed date or a "
                                     "priority downgrade", {"type": "escalate"}, o))
    return {"valid": not issues, "issues": issues, "healable": all(i["healable"] for i in issues)}


# --------------------------------------------------------------------------- dispatch
def _reserve(sku: str, origin: str, qty: int) -> bool:
    """Atomic check-and-decrement: two concurrent orders can never both take the last units."""
    with _INV_LOCK:
        if data.INVENTORY[sku].get(origin, 0) < qty:
            return False
        data.INVENTORY[sku][origin] -= qty
        return True


def _release(sku: str, origin: str, qty: int) -> None:
    with _INV_LOCK:
        data.INVENTORY[sku][origin] += qty


def _book_leg(leg: dict, order: dict, auctions: dict, heal_log: list | None) -> dict:
    o = leg["origin"]
    if not _reserve(order["sku"], o, leg["quantity"]):
        log_event(heal_log, "stock_conflict", f"{o}: stock taken by a concurrent order before booking")
        return {"failed": {"origin": o, "carrier": leg["carrier"], "reason": "stock no longer available"}}
    ranked = (auctions.get(o) or {}).get("ranked", [])
    primary = next((q for q in ranked if q["carrier"] == leg["carrier"]), None)
    options = ([primary] if primary else []) + [q for q in ranked if q["sla_ok"] and q is not primary]
    for q in options:
        c = q["carrier"]
        if not BREAKER.allow(c):
            log_event(heal_log, "circuit_open", f"booking skipped {c} (circuit open)")
            continue
        try:
            booked = retry(lambda a, c=c, q=q: api_book(c, q, order["order_id"], key=f"{o}:{c}:{a}"),
                           attempts=2, label=f"{c}.book", heal_log=heal_log, retry_on=(CarrierAPIError,))
        except CarrierAPIError as e:
            BREAKER.failure(c)
            log_event(heal_log, "booking_failed", f"{c} booking failed: {e}")
            continue
        BREAKER.success(c)
        if c != leg["carrier"]:
            log_event(heal_log, "carrier_failover", f"{o}: {leg['carrier']} -> {c}")
        rec = {**booked, "order_id": order["order_id"], "sku": order["sku"], "quantity": leg["quantity"],
               "origin": o, "destination": order["destination_city"]}
        store.save_shipment(rec)
        return {"shipment": rec}
    _release(order["sku"], o, leg["quantity"])
    return {"failed": {"origin": o, "carrier": leg["carrier"], "reason": "all SLA-safe carriers failed"}}


def dispatch(plan: dict, order: dict, auctions: dict, heal_log: list | None = None) -> dict:
    """Book every leg (legs in parallel). Fails over to the next SLA-compliant carrier on booking errors.

    Idempotent per (order, origin): a leg that was already booked is returned, never booked twice.
    """
    already = {s["origin"]: s for s in store.find_shipments(order["order_id"])}
    shipments, failed, todo = [], [], []
    for leg in plan["legs"]:
        if leg["origin"] in already:
            log_event(heal_log, "idempotent_skip", f"{leg['origin']} already booked for {order['order_id']}")
            shipments.append(already[leg["origin"]])
        else:
            todo.append(leg)
    if len(todo) > 1:
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            outcomes = list(ex.map(lambda leg: _book_leg(leg, order, auctions, heal_log), todo))
    else:
        outcomes = [_book_leg(leg, order, auctions, heal_log) for leg in todo]
    for r in outcomes:
        (shipments.append(r["shipment"]) if "shipment" in r else failed.append(r["failed"]))
    status = "DISPATCHED" if not failed else ("PARTIAL" if shipments else "FAILED")
    return {"status": status, "shipments": shipments, "failed_legs": failed,
            "total_cost_usd": round(sum(s["price_usd"] for s in shipments), 2),
            "max_eta_days": max((s["eta_days"] for s in shipments), default=None)}


# --------------------------------------------------------------------------- review (validator policy)
def format_feedback(issues: list[dict]) -> str:
    return "FEEDBACK:\n" + "\n".join(
        f"[leg {i.get('leg', '-')}] {i['code']}: {i['message']} -> {i['suggestion']}" for i in issues)


def _signature(issues: list[dict]) -> list:
    return sorted([i["code"], i.get("leg", "")] for i in issues)


def review_plan(plan: dict, order: dict, auctions: dict, *, iteration: int, max_iterations: int,
                previous_issues: list[dict] | None = None, heal_log: list | None = None) -> dict:
    """The validator's decision procedure: validate -> dispatch -> stop, or structured feedback to re-plan.

    Deterministic on purpose: whether an order is booked must never depend on an LLM remembering to call
    the right tool. Decisions: dispatched | partial | escalated (terminal) or feedback (loop again).
    Escalates early when issues are unhealable, when a re-plan made no progress, or on the last iteration.
    """
    validation = validate_plan(plan, order, auctions) if plan.get("legs") is not None else {
        "valid": False, "healable": True, "issues": [_issue("no_plan", "no plan submitted",
                                                            "planner must call submit_plan", {"type": "replan"})]}
    result = {"iteration": iteration, "validation": validation, "dispatch": None, "issues": validation["issues"]}
    if validation["valid"]:
        d = dispatch(plan, order, auctions, heal_log=heal_log)
        result["dispatch"] = d
        ids = ", ".join(s["tracking_id"] for s in d["shipments"])
        if d["status"] == "DISPATCHED":
            return {**result, "decision": "dispatched", "terminal": True,
                    "reason": f"DISPATCHED {ids}; total ${d['total_cost_usd']}, eta {d['max_eta_days']}d"}
        if d["status"] == "PARTIAL":
            legs = ", ".join(f"{f['origin']} ({f['reason']})" for f in d["failed_legs"])
            log_event(heal_log, "escalated", f"partial dispatch; ops must rebook: {legs}")
            return {**result, "decision": "partial", "terminal": True,
                    "reason": f"PARTIAL: booked {ids}; ESCALATED to ops for {legs}"}
        result["issues"] = [_issue("dispatch_failed", f"booking failed at {f['origin']}: {f['reason']}",
                                   f"run_carrier_auction for {f['origin']} again",
                                   {"type": "rerun_auction"}, f["origin"]) for f in d["failed_legs"]]
    issues = result["issues"]
    for i in issues:
        log_event(heal_log, "plan_rejected", f"{i['code']}: {i['message']} -> {i['suggestion']}")

    if not all(i["healable"] for i in issues):
        reason = "ESCALATED: " + "; ".join(i["message"] for i in issues if not i["healable"])
    elif previous_issues is not None and _signature(previous_issues) == _signature(issues):
        reason = "ESCALATED: re-plan made no progress (same issues twice): " + \
                 "; ".join(f"{i['code']}@{i.get('leg', '-')}" for i in issues)
    elif iteration >= max_iterations:
        reason = f"ESCALATED: no valid plan after {iteration} iterations"
    else:
        return {**result, "decision": "feedback", "terminal": False, "reason": format_feedback(issues)}
    log_event(heal_log, "escalated", reason)
    return {**result, "decision": "escalated", "terminal": True, "reason": reason}
