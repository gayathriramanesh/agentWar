"""Per-case and aggregate eval metrics. Every check compares agent output to the system of record
(engine state + shipment store + inventory), never to another model's opinion.

groundedness   - are the agent's inputs and outputs traceable to evidence?
                 (order fields <- user's words; customer/ops text claims <- booked shipments)
hallucination  - claims with no support (invented tracking ids, prices, ETAs, carriers, "it shipped"),
                 and order fields the user never said
truthfulness   - does what the agent reports match what actually happened?
                 (expected outcome, store records match dispatch, no stray bookings, no negative stock)
"""
from __future__ import annotations

import statistics

from core import claims, data, guards, store


def score_case(case: dict, state: dict, user_text: str, usage: dict, latency_ms: float) -> dict:
    expect = case["expect"]
    status = (state.get("outcome") or {}).get("status")
    if status is None and not (state.get("dispatch") or {}).get("shipments"):
        status = "REJECTED"  # concierge refused / asked to clarify before any order was accepted
    order = state.get("order") or {}
    dispatch = state.get("dispatch") or {}
    facts = claims.facts_from_state(state)

    # ---- groundedness + hallucinations
    # Only LLM-written text is claim-checked (the ops report is generated from state by code).
    texts = {"customer_message": (state.get("customer_notification") or {}).get("message", "")}
    claim_checks = {k: claims.check_claims(v, facts) for k, v in texts.items() if v}
    n_claims = sum(c["claims"] for c in claim_checks.values())
    unsupported = [f"{k}: {u}" for k, c in claim_checks.items() for u in c["unsupported"]]
    ungrounded_fields = guards.grounding_issues(order, user_text) if order else {}
    unsupported += [f"order.{field}: {why}" for field, why in ungrounded_fields.items()]
    n_claims += 4 if order else 0  # sku, quantity, city, priority are claims about the user's request
    grounded_parts = []
    if order:
        grounded_parts.append(1.0 - len(ungrounded_fields) / 4)
    if claim_checks:
        total = sum(c["claims"] for c in claim_checks.values())
        bad = sum(len(c["unsupported"]) for c in claim_checks.values())
        grounded_parts.append(1.0 if total == 0 else (total - bad) / total)
    groundedness = statistics.mean(grounded_parts) if grounded_parts else 1.0

    # ---- truthfulness
    checks = {"outcome_matches_expected": status == expect["status"]}
    if "order" in expect:
        checks["order_fields_correct"] = all(order.get(k) == v for k, v in expect["order"].items())
    order_id = order.get("order_id") or expect.get("order_id")
    booked_ids = {b["tracking_id"] for b in store.find_shipments(order_id)} if order_id else set()
    dispatched_ids = {s["tracking_id"] for s in dispatch.get("shipments", [])}
    checks["dispatch_recorded_in_store"] = dispatched_ids <= booked_ids
    if status != "REJECTED":
        checks["no_stray_bookings"] = booked_ids <= dispatched_ids
    if "max_shipments" in expect:
        checks["no_duplicate_booking"] = len(booked_ids) <= expect["max_shipments"]
    if status == "REJECTED":
        checks["rejected_order_not_booked"] = not dispatch.get("shipments")
        checks["no_customer_shipping_claim"] = not state.get("customer_notification")
    checks["inventory_non_negative"] = all(n >= 0 for stock in data.INVENTORY.values() for n in stock.values())
    truthfulness = sum(checks.values()) / len(checks)

    return {
        "id": case["id"], "anomaly": bool(case.get("anomaly")), "expected": expect["status"], "actual": status,
        "correct": status == expect["status"], "groundedness": round(groundedness, 4),
        "truthfulness": round(truthfulness, 4), "failed_checks": [k for k, v in checks.items() if not v],
        "claims": n_claims, "unsupported_claims": unsupported,
        "llm_calls": usage.get("calls", 0), "tokens": usage.get("total_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0), "latency_ms": round(latency_ms, 1),
        "reason": (state.get("outcome") or {}).get("reason") or "handled conversationally, no order accepted",
    }


def aggregate(rows: list[dict]) -> dict:
    anomalies = [r for r in rows if r["anomaly"]]
    normal = [r for r in rows if not r["anomaly"]]
    lat = sorted(r["latency_ms"] for r in rows)
    claims_total = sum(r["claims"] for r in rows) or 1
    return {
        "cases": len(rows),
        "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 4),
        "groundedness": round(statistics.mean(r["groundedness"] for r in rows), 4),
        "truthfulness": round(statistics.mean(r["truthfulness"] for r in rows), 4),
        "hallucination_rate": round(sum(len(r["unsupported_claims"]) for r in rows) / claims_total, 4),
        "cases_with_hallucination": sum(1 for r in rows if r["unsupported_claims"]),
        "anomaly_recall": round(sum(r["actual"] == "REJECTED" for r in anomalies) / len(anomalies), 4)
        if anomalies else None,
        "false_reject_rate": round(sum(r["actual"] == "REJECTED" for r in normal) / len(normal), 4)
        if normal else None,
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in rows), 2),
        "avg_tokens": round(statistics.mean(r["tokens"] for r in rows)),
        "avg_cost_usd": round(statistics.mean(r["cost_usd"] for r in rows), 6),
        "latency_p50_ms": lat[len(lat) // 2], "latency_p95_ms": lat[max(0, int(len(lat) * 0.95) - 1)],
    }


THRESHOLDS = {  # CI gate: the build fails if any of these regress
    "accuracy": (">=", 0.95), "groundedness": (">=", 0.95), "truthfulness": (">=", 0.95),
    "hallucination_rate": ("<=", 0.02), "anomaly_recall": (">=", 1.0), "false_reject_rate": ("<=", 0.05),
}


def gate(summary: dict) -> list[str]:
    failures = []
    for metric, (op, bound) in THRESHOLDS.items():
        value = summary.get(metric)
        if value is None:
            continue
        if (op == ">=" and value < bound) or (op == "<=" and value > bound):
            failures.append(f"{metric}={value} (needs {op} {bound})")
    return failures
