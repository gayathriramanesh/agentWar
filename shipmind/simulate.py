#!/usr/bin/env python3
"""Scale + chaos simulator. Pushes many concurrent orders through the same engine the agents use
(allocation -> parallel carrier auction -> validate/heal loop -> dispatch with fail-over).
No API key and no ADK needed; the LLM reasoning steps are replaced by their deterministic policy.

  python simulate.py                                  # 200 orders, 32 workers, 20% chaos
  python simulate.py --orders 1000 --workers 64 --chaos 0.35
"""
from __future__ import annotations

import argparse
import copy
import random
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from core import config, data, logistics, store
from core.carrier_apis import Chaos
from core.healing import BREAKER, log_event


def simulated_model(role: str, key: str, heal_log: list) -> str:
    """Model tier selection + fallback chain, with chaos-injected 429s on the model API."""
    for round_ in range(2):  # walk the chain, then retry it once
        for tier in config.FALLBACK_CHAIN[role]:
            model = config.MODELS[tier]
            if Chaos.should_fail("llm", f"{model}|{key}|{round_}"):
                log_event(heal_log, "model_fallback", f"{model} 429 -> next model")
                continue
            return model
    log_event(heal_log, "degraded_to_rules", "all models unavailable -> deterministic policy engine")
    return "rules-engine (degraded)"


def plan_carriers(alloc: dict, auctions: dict, issues: list[dict], greedy: bool) -> list[str]:
    """Planner policy: follow the validator's machine-readable actions, else best (or cheapest as a draft)."""
    overrides = {i["leg"]: i["action"]["carrier"] for i in issues if i["action"]["type"] == "use_carrier"}
    carriers = []
    for a in alloc["allocations"]:
        auc = auctions[a["warehouse"]]
        pick = (auc["cheapest"] if greedy else auc["best"]) or auc["cheapest"]
        carriers.append(overrides.get(a["warehouse"]) or (pick["carrier"] if pick else "UPS"))
    return carriers


def process(order_spec: tuple, greedy_draft: bool) -> dict:
    t0, heal = time.perf_counter(), []
    oid, sku, qty, city, prio = order_spec
    order = logistics.normalize_order(oid, sku, qty, city, prio)
    planner_model = simulated_model(config.route_for_complexity(order["complexity"]), oid, heal)
    alloc = logistics.allocate_inventory(sku, qty, city)
    if alloc["status"] != "ok":
        return {"id": oid, "status": "ESCALATED_BACKORDER", "heal": heal, "iterations": 0,
                "ms": (time.perf_counter() - t0) * 1000, "model": planner_model, "complexity": order["complexity"]}
    review, auctions = None, {}
    for it in range(1, config.MAX_HEAL_ITERATIONS + 1):
        issues = review["issues"] if review else []
        realloc = next((i["action"] for i in issues if i["action"]["type"] == "reallocate"), None)
        if realloc:
            alloc = logistics.allocate_inventory(sku, qty, city, avoid_disrupted=realloc["avoid_disrupted"])
        for a in alloc["allocations"]:
            auctions[a["warehouse"]] = logistics.carrier_auction(
                a["warehouse"], city, a["quantity"] * order["unit_weight_kg"], prio, order_id=f"{oid}:{it}", heal_log=heal)
        carriers = plan_carriers(alloc, auctions, issues, greedy_draft and it == 1)
        plan = logistics.build_plan(order, [a["warehouse"] for a in alloc["allocations"]],
                                    [a["quantity"] for a in alloc["allocations"]], carriers, auctions)
        review = logistics.review_plan(plan, order, auctions, iteration=it, max_iterations=config.MAX_HEAL_ITERATIONS,
                                       previous_issues=issues if it > 1 else None, heal_log=heal)
        if review["terminal"]:
            break
    result = review["dispatch"] if review["decision"] in ("dispatched", "partial") else None
    if result:
        status = result["status"]
    else:
        codes = {i["code"] for i in review["issues"]}
        status = "ESCALATED_BACKORDER" if "insufficient_stock" in codes else "ESCALATED_SLA_INFEASIBLE" \
            if "sla_infeasible" in codes else "ESCALATED_NO_PROGRESS"
    return {"id": oid, "status": status, "heal": heal, "iterations": it, "model": planner_model,
            "complexity": order["complexity"], "ms": (time.perf_counter() - t0) * 1000,
            "cost": result and result["total_cost_usd"]}


def random_orders(n: int, seed: int) -> list[tuple]:
    rng = random.Random(seed)
    skus, cities = list(data.INVENTORY), list(data.CITIES)
    return [(f"ORD-{10000 + i}", rng.choice(skus), rng.choice([1, 1, 1, 2, 3, 5]), rng.choice(cities),
             rng.choices(list(data.PRIORITY), weights=[3, 5, 2])[0]) for i in range(n)]


def run_simulation(orders: int = 200, workers: int = 32, chaos: float = 0.2, seed: int = 7,
                   greedy_draft: bool = True) -> dict:
    """Run a batch and return summary stats (used by the CLI and the demo dashboard)."""
    Chaos.configure(chaos, seed)
    BREAKER.reset()
    logistics.clear_quote_cache()
    snapshot = copy.deepcopy(data.INVENTORY)
    for stock in data.INVENTORY.values():  # scale the network up for a big batch
        for wh in stock:
            stock[wh] *= max(1, orders // 20)
    batch = random_orders(orders, seed)
    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda o: process(o, greedy_draft), batch))
    finally:
        wall = time.perf_counter() - t0
        data.INVENTORY.clear()
        data.INVENTORY.update(snapshot)
        Chaos.configure(config.CHAOS_RATE, config.CHAOS_SEED)
        BREAKER.reset()
    lat = sorted(r["ms"] for r in results)
    statuses = Counter(r["status"] for r in results)
    return {
        "orders": orders, "workers": workers, "chaos": chaos,
        "outcomes": dict(statuses.most_common()),
        "heals": dict(Counter(h["kind"] for r in results for h in r["heal"]).most_common()),
        "routing": dict(Counter(f"{r['complexity']} -> {r['model']}" for r in results).most_common()),
        "wall_s": round(wall, 2), "throughput": round(len(results) / wall, 1),
        "p50_ms": round(statistics.median(lat)), "p95_ms": round(lat[int(len(lat) * 0.95) - 1]),
        "avg_iterations": round(statistics.mean(r["iterations"] for r in results), 2),
        "fulfilled": statuses["DISPATCHED"] + statuses["PARTIAL"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orders", type=int, default=200)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--chaos", type=float, default=0.2, help="failure probability per external call")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-greedy-draft", action="store_true", help="planner picks best carrier on first pass")
    args = ap.parse_args()
    store.reset()
    print(f"ShipMind load simulation: {args.orders} orders | {args.workers} workers | chaos {args.chaos:.0%}")
    r = run_simulation(args.orders, args.workers, args.chaos, args.seed, not args.no_greedy_draft)
    print("\n── Outcomes " + "─" * 50)
    for k, v in r["outcomes"].items():
        print(f"  {k:<26}{v:>6}  ({v / r['orders']:.1%})")
    print("── Self-healing events " + "─" * 39)
    for k, v in r["heals"].items():
        print(f"  {k:<26}{v:>6}")
    print("── Model routing " + "─" * 45)
    for k, v in r["routing"].items():
        print(f"  {k:<40}{v:>6}")
    print("── Performance " + "─" * 47)
    print(f"  wall time            {r['wall_s']:8.2f} s   throughput {r['throughput']:7.1f} orders/s")
    print(f"  latency p50 / p95    {r['p50_ms']:6d} / {r['p95_ms']} ms per order")
    print(f"  heal-loop iterations avg {r['avg_iterations']:.2f}")
    print(f"  fulfilled            {r['fulfilled']}/{r['orders']} despite {args.chaos:.0%} injected API failures")


if __name__ == "__main__":
    main()
