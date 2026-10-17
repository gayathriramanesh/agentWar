"""Mock carrier REST APIs (quote + book) with realistic latency and injectable failures (chaos)."""
from __future__ import annotations

import hashlib
import math
import random
import time

from . import config
from .data import CARRIERS, DISRUPTIONS, PRIORITY, distance_km


class CarrierAPIError(RuntimeError):
    """Raised for transient upstream failures (HTTP 429/503/timeouts)."""


class Chaos:
    """Deterministic fault injection: same seed + same call key => same outcome, even across threads."""

    rate: float = config.CHAOS_RATE
    seed: int = config.CHAOS_SEED

    @classmethod
    def configure(cls, rate: float, seed: int | None = None) -> None:
        cls.rate = rate
        if seed is not None:
            cls.seed = seed

    @classmethod
    def should_fail(cls, name: str, key: str) -> bool:
        if cls.rate <= 0:
            return False
        p = min(0.9, cls.rate * CARRIERS.get(name, {}).get("flaky", 1.0))
        return random.Random(f"{cls.seed}|{name}|{key}").random() < p


_ERRORS = ["HTTP 503 Service Unavailable", "HTTP 429 Too Many Requests", "read timeout after 2000ms"]


def _call(name: str, key: str) -> None:
    time.sleep(config.MOCK_LATENCY_MS / 1000)
    if Chaos.should_fail(name, key):
        idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(_ERRORS)
        raise CarrierAPIError(f"{name}: {_ERRORS[idx]}")


def quote(carrier: str, origin: str, dest_city: str, weight_kg: float, priority: str, key: str) -> dict:
    """POST /v1/rates  (mock)"""
    _call(carrier, f"quote:{key}")
    c, p = CARRIERS[carrier], PRIORITY[priority]
    base = {"carrier": carrier, "origin": origin, "destination": dest_city}
    parcels = max(1, math.ceil(weight_kg / c["max_kg"]))  # multi-piece shipment above the parcel limit
    if parcels > 4:
        return {**base, "available": False, "reason": f"freight too heavy ({weight_kg:.1f} kg, > 4 parcels)"}
    dist = distance_km(origin, dest_city)
    transit = max(1, math.ceil(dist / (c["km_per_day"] * p["speed"])))
    delay = DISRUPTIONS.get(origin, {}).get("delay_days", 0)
    per_parcel = weight_kg / parcels
    price = parcels * (c["base"] + c["per_km"] * dist) * (1 + per_parcel / 20) * p["price_mult"]
    return {
        **base,
        "available": True,
        "distance_km": dist,
        "eta_days": transit + p["handling_days"] + delay,
        "disruption_delay_days": delay,
        "price_usd": round(price, 2),
        "parcels": parcels,
        "reliability": c["reliability"],
    }


def book(carrier: str, quote_: dict, order_id: str, key: str) -> dict:
    """POST /v1/shipments  (mock) -> label + tracking id"""
    _call(carrier, f"book:{key}")
    tid = "SM-" + carrier[:3].upper() + "-" + hashlib.sha1(f"{order_id}|{key}".encode()).hexdigest()[:8].upper()
    return {"tracking_id": tid, "carrier": carrier, "label_url": f"https://labels.mock/{tid}.pdf",
            "eta_days": quote_["eta_days"], "price_usd": quote_["price_usd"]}
