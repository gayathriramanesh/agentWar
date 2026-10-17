import copy

import pytest

import threading

from core import data, logistics, store
from core.carrier_apis import Chaos
from core.healing import BREAKER


@pytest.fixture(autouse=True)
def clean():
    inv = copy.deepcopy(data.INVENTORY)
    Chaos.configure(0.0, 42)
    BREAKER.reset()
    logistics.clear_quote_cache()
    store.reset()
    yield
    data.INVENTORY.clear()
    data.INVENTORY.update(inv)
    Chaos.configure(0.0, 42)
    BREAKER.reset()


def test_auction_best_is_sla_compliant_and_cheapest_is_not():
    a = logistics.carrier_auction("SEA1", "Boston", 0.3, "economy")
    assert a["best"]["sla_ok"] and a["best"]["carrier"] == "UPS"
    assert a["cheapest"]["carrier"] == "USPS" and not a["cheapest"]["sla_ok"]


def test_validator_rejects_greedy_plan_with_fix():
    order = logistics.normalize_order("T1", "SKU-KINDLE", 1, "Boston", "economy")
    auctions = {"SEA1": logistics.carrier_auction("SEA1", "Boston", 0.3, "economy")}
    bad = logistics.build_plan(order, ["SEA1"], [1], ["USPS"], auctions)
    v = logistics.validate_plan(bad, order, auctions)
    assert not v["valid"] and v["issues"][0]["code"] == "sla_breach" and "UPS" in v["issues"][0]["suggestion"]
    good = logistics.build_plan(order, ["SEA1"], [1], ["UPS"], auctions)
    assert logistics.validate_plan(good, order, auctions)["valid"]


def test_storm_origin_triggers_reallocation():
    order = logistics.normalize_order("T2", "SKU-LAPTOP", 3, "Austin", "express")
    auctions = {"DFW7": logistics.carrier_auction("DFW7", "Austin", 7.5, "express")}
    plan = logistics.build_plan(order, ["DFW7"], [3], ["UPS"], auctions)
    assert logistics.validate_plan(plan, order, auctions)["issues"][0]["code"] == "sla_breach_origin"
    assert logistics.allocate_inventory("SKU-LAPTOP", 3, "Austin", avoid_disrupted=True)["allocations"][0]["warehouse"] == "ORD4"


def test_chaos_retries_and_failover_still_dispatch():
    Chaos.configure(0.4, 3)
    heal = []
    order = logistics.normalize_order("T3", "SKU-ECHO", 2, "Miami", "express")
    alloc = logistics.allocate_inventory("SKU-ECHO", 2, "Miami")["allocations"][0]
    auctions = {alloc["warehouse"]: logistics.carrier_auction(alloc["warehouse"], "Miami", 1.8, "express", "T3", heal)}
    best = auctions[alloc["warehouse"]]["best"]
    plan = logistics.build_plan(order, [alloc["warehouse"]], [2], [best["carrier"]], auctions)
    result = logistics.dispatch(plan, order, auctions, heal)
    assert result["status"] == "DISPATCHED"
    assert any(h["kind"] in {"retry", "carrier_failover", "carrier_unreachable"} for h in heal)


def test_bad_input_returns_valid_options_for_llm_self_correction():
    r = logistics.normalize_order("T4", "SKU-NOPE", 1, "Gotham", "warp")
    assert set(r["valid_options"]) == {"sku", "destination_city", "priority"}


def test_heavy_freight_is_multi_parcel():
    q = logistics.carrier_auction("ORD4", "Chicago", 75.0, "standard")["best"]
    assert q["parcels"] >= 2


# ---------------------------------------------------------------- anomaly detection on engine inputs
@pytest.mark.parametrize("qty", [2.7, True, -5, 0, "abc", float("nan"), 10**9])
def test_anomalous_quantities_are_rejected_not_coerced(qty):
    assert "error" in logistics.normalize_order("T5", "SKU-ECHO", qty, "Boston", "standard")
    assert "error" in logistics.allocate_inventory("SKU-ECHO", qty, "Boston")


def test_bad_auction_inputs_return_errors_instead_of_raising():
    assert "error" in logistics.carrier_auction("XXX", "Boston", 1, "standard")
    assert "error" in logistics.carrier_auction("SEA1", "Boston", -50, "standard")


def test_anomalous_quote_is_dropped(monkeypatch):
    real = logistics.api_quote
    monkeypatch.setattr(logistics, "api_quote", lambda c, *a, **k: {**real(c, *a, **k), "price_usd": -1.0}
                        if c == "USPS" else real(c, *a, **k))
    heal = []
    a = logistics.carrier_auction("SEA1", "Boston", 0.3, "economy", heal_log=heal)
    assert "USPS" not in {q["carrier"] for q in a["ranked"]}
    assert any(h["kind"] == "quote_anomaly" for h in heal)


def test_plan_with_duplicate_negative_or_stale_legs_is_invalid():
    order = logistics.normalize_order("T6", "SKU-TV65", 1, "Chicago", "standard")
    auctions = {"ORD4": logistics.carrier_auction("ORD4", "Chicago", 15, "standard")}
    dup = logistics.validate_plan(logistics.build_plan(order, ["ORD4", "ORD4"], [3, -2], ["UPS", "UPS"], auctions),
                                  order, auctions)
    assert {"duplicate_origin", "bad_quantity"} <= {i["code"] for i in dup["issues"]}
    order4 = {**order, "quantity": 4}
    stale = logistics.validate_plan(logistics.build_plan(order4, ["ORD4"], [4], ["UPS"], auctions), order4, auctions)
    assert [i["code"] for i in stale["issues"]] == ["stale_quote"]


def test_dispatch_is_idempotent_and_never_oversells():
    order = logistics.normalize_order("T7", "SKU-TV65", 1, "Chicago", "standard")
    auctions = {"ORD4": logistics.carrier_auction("ORD4", "Chicago", 15, "standard")}
    plan = logistics.build_plan(order, ["ORD4"], [1], [auctions["ORD4"]["best"]["carrier"]], auctions)
    for _ in range(5):
        logistics.dispatch(plan, order, auctions)
    assert data.INVENTORY["SKU-TV65"]["ORD4"] == 3 and len(store.find_shipments("T7")) == 1
    assert "duplicate" in logistics.normalize_order("T7", "SKU-TV65", 1, "Chicago", "standard")["error"]


def test_concurrent_orders_cannot_take_the_same_last_units():
    data.INVENTORY["SKU-TV65"]["ORD4"] = 1
    auctions = {"ORD4": logistics.carrier_auction("ORD4", "Chicago", 15, "standard")}
    results = []

    def go(i):
        order = {**logistics.normalize_order(f"C{i}", "SKU-TV65", 1, "Chicago", "standard"), "order_id": f"C{i}"}
        plan = logistics.build_plan(order, ["ORD4"], [1], [auctions["ORD4"]["best"]["carrier"]], auctions)
        results.append(logistics.dispatch(plan, order, auctions)["status"])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("DISPATCHED") == 1 and data.INVENTORY["SKU-TV65"]["ORD4"] == 0


def test_review_escalates_when_replan_makes_no_progress():
    order = logistics.normalize_order("T8", "SKU-KINDLE", 1, "Boston", "economy")
    auctions = {"SEA1": logistics.carrier_auction("SEA1", "Boston", 0.3, "economy")}
    bad = logistics.build_plan(order, ["SEA1"], [1], ["USPS"], auctions)
    first = logistics.review_plan(bad, order, auctions, iteration=1, max_iterations=3)
    assert first["decision"] == "feedback" and first["issues"][0]["action"] == {"type": "use_carrier", "carrier": "UPS"}
    second = logistics.review_plan(bad, order, auctions, iteration=2, max_iterations=3, previous_issues=first["issues"])
    assert second["decision"] == "escalated" and "no progress" in second["reason"]


def test_circuit_breaker_half_open_allows_a_single_probe():
    cb = type(BREAKER)(threshold=1, cooldown_s=0.0)
    cb.failure("X")
    assert cb.allow("X") is True and cb.allow("X") is False  # one probe in flight, others wait
    cb.success("X")
    assert cb.allow("X") is True


def test_quote_cache_serves_repeat_auctions():
    logistics.carrier_auction("SEA1", "Boston", 0.3, "economy")
    again = logistics.carrier_auction("SEA1", "Boston", 0.3, "economy")
    assert again["cache_hits"] == len(data.CARRIERS)
