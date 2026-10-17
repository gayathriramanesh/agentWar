import copy

import pytest

from core import data, logistics
from core.carrier_apis import Chaos
from core.healing import BREAKER


@pytest.fixture(autouse=True)
def clean():
    inv = copy.deepcopy(data.INVENTORY)
    Chaos.configure(0.0, 42)
    BREAKER.reset()
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
