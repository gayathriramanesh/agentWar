"""Mock 'world' for ShipMind: warehouses, inventory, carriers, cities and live disruptions.

Swap these dicts for real WMS / TMS / carrier APIs later; every consumer goes through core.logistics.
"""
from __future__ import annotations

import math

WAREHOUSES = {
    "SEA1": {"city": "Seattle", "lat": 47.61, "lon": -122.33},
    "SFO2": {"city": "San Francisco", "lat": 37.77, "lon": -122.42},
    "DFW7": {"city": "Dallas", "lat": 32.78, "lon": -96.80},
    "ORD4": {"city": "Chicago", "lat": 41.88, "lon": -87.63},
    "JFK8": {"city": "New York", "lat": 40.71, "lon": -74.01},
    "ATL2": {"city": "Atlanta", "lat": 33.75, "lon": -84.39},
}

CITIES = {
    "Seattle": (47.61, -122.33), "San Francisco": (37.77, -122.42), "Dallas": (32.78, -96.80),
    "Chicago": (41.88, -87.63), "New York": (40.71, -74.01), "Atlanta": (33.75, -84.39),
    "Miami": (25.76, -80.19), "Boston": (42.36, -71.06), "Austin": (30.27, -97.74),
    "Denver": (39.74, -104.99), "Phoenix": (33.45, -112.07), "Portland": (45.52, -122.68),
    "Los Angeles": (34.05, -118.24), "Houston": (29.76, -95.37),
}

# sku -> warehouse -> units on hand
INVENTORY = {
    "SKU-ECHO": {"SEA1": 120, "SFO2": 10, "DFW7": 80, "ORD4": 60, "JFK8": 0, "ATL2": 40},
    "SKU-KINDLE": {"SEA1": 30, "SFO2": 50, "DFW7": 0, "ORD4": 0, "JFK8": 0, "ATL2": 0},
    "SKU-LAPTOP": {"SEA1": 5, "SFO2": 8, "DFW7": 25, "ORD4": 12, "JFK8": 3, "ATL2": 0},
    "SKU-TV65": {"SEA1": 0, "SFO2": 0, "DFW7": 6, "ORD4": 4, "JFK8": 0, "ATL2": 0},
}
SKU_WEIGHT_KG = {"SKU-ECHO": 0.9, "SKU-KINDLE": 0.3, "SKU-LAPTOP": 2.5, "SKU-TV65": 15.0}

# Carrier economics. `flaky` scales how often the mock API fails under chaos.
CARRIERS = {
    "AmazonFleet": {"base": 4.0, "per_km": 0.0030, "km_per_day": 700, "reliability": 0.95, "max_kg": 30, "flaky": 1.2},
    "FedEx": {"base": 9.0, "per_km": 0.0045, "km_per_day": 1400, "reliability": 0.98, "max_kg": 70, "flaky": 1.0},
    "UPS": {"base": 7.5, "per_km": 0.0038, "km_per_day": 1100, "reliability": 0.97, "max_kg": 70, "flaky": 1.0},
    "DHL": {"base": 8.0, "per_km": 0.0035, "km_per_day": 900, "reliability": 0.94, "max_kg": 50, "flaky": 1.5},
    "USPS": {"base": 5.0, "per_km": 0.0025, "km_per_day": 500, "reliability": 0.92, "max_kg": 30, "flaky": 1.0},
}

PRIORITY = {
    "express": {"speed": 2.0, "handling_days": 0, "price_mult": 1.8, "sla_days": 2},
    "standard": {"speed": 1.0, "handling_days": 1, "price_mult": 1.0, "sla_days": 5},
    "economy": {"speed": 0.7, "handling_days": 1, "price_mult": 0.75, "sla_days": 8},
}

# Scoring weights per priority (lower score wins).
SCORE_WEIGHTS = {
    "express": {"price": 0.2, "eta": 0.6, "risk": 0.2},
    "standard": {"price": 0.45, "eta": 0.35, "risk": 0.2},
    "economy": {"price": 0.7, "eta": 0.1, "risk": 0.2},
}

# Live disruption feed (weather, labor, outages). Adds delay to anything leaving that site.
DISRUPTIONS = {
    "DFW7": {"type": "severe_storm", "severity": "high", "delay_days": 2},
    "JFK8": {"type": "labor_shortage", "severity": "medium", "delay_days": 1},
}


def distance_km(origin_wh: str, city: str) -> float:
    w = WAREHOUSES[origin_wh]
    lat2, lon2 = CITIES[city]
    p1, p2 = math.radians(w["lat"]), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - w["lon"])
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(a)), 1)
