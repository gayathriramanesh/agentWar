"""Central configuration. Every knob is an env var so the team can tune without code changes."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv ships with google-adk; harmless if missing.
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: str, cast=int, lo: float | None = None, hi: float | None = None):
    """Parse a numeric env var and fail fast at startup on nonsense instead of misbehaving at runtime."""
    raw = os.getenv(name, default).strip()
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a valid {cast.__name__}") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(f"{name}={value} is outside [{lo}, {hi}]")
    return value


# Model tiers. Verify IDs in Google AI Studio's model list and override in .env if needed.
MODELS = {
    "fast": os.getenv("SHIPMIND_FAST_MODEL", "gemini-2.5-flash"),
    "balanced": os.getenv("SHIPMIND_BALANCED_MODEL", "gemini-3.5-flash"),
    "deep": os.getenv("SHIPMIND_DEEP_MODEL", "gemini-3.1-pro-preview"),
}

# Self-healing model chains: on 429/5xx the next model in the chain answers instead.
FALLBACK_CHAIN = {
    "fast": ["fast", "balanced"],
    "balanced": ["balanced", "fast"],
    "deep": ["deep", "balanced", "fast"],
}

USE_MCP = _flag("SHIPMIND_USE_MCP", "true")

# Offline mode: scripted LLM drives the real ADK runtime. "auto" = on when no Gemini credentials exist.
_mock = os.getenv("SHIPMIND_MOCK_LLM", "auto").strip().lower()
HAS_GEMINI_CREDS = bool(os.getenv("GOOGLE_API_KEY")) or _flag("GOOGLE_GENAI_USE_VERTEXAI", "false")
MOCK_LLM = (not HAS_GEMINI_CREDS) if _mock == "auto" else _mock in {"1", "true", "yes", "on"}
# Mock planner drafts the cheapest plan first so the validator->heal loop is visible in demos.
MOCK_GREEDY_DRAFT = _flag("SHIPMIND_MOCK_GREEDY_DRAFT", "true")
MAX_HEAL_ITERATIONS = _num("SHIPMIND_MAX_HEAL_ITERATIONS", "3", lo=1, hi=10)
MOCK_LATENCY_MS = _num("SHIPMIND_MOCK_LATENCY_MS", "40", lo=0)
CHAOS_RATE = _num("SHIPMIND_CHAOS_RATE", "0", float, lo=0, hi=1)
CHAOS_SEED = _num("SHIPMIND_CHAOS_SEED", "42")
# Guardrails: anything outside these bounds is treated as an anomaly and rejected, never "fixed" silently.
MAX_UNITS_PER_ORDER = _num("SHIPMIND_MAX_UNITS_PER_ORDER", "500", lo=1)
MAX_INPUT_CHARS = _num("SHIPMIND_MAX_INPUT_CHARS", "2000", lo=100)
# Latency budget: a carrier that hasn't answered by the deadline is treated as failed for this auction.
AUCTION_TIMEOUT_MS = _num("SHIPMIND_AUCTION_TIMEOUT_MS", "3000", lo=50)
# Per-call LLM timeout so a hung model request can't stall an order forever.
LLM_TIMEOUT_S = _num("SHIPMIND_LLM_TIMEOUT_S", "30", lo=1)
# Input guardrail agent: if its verdict is missing (model outage), fail closed (reject) unless told otherwise.
GUARDRAIL_FAIL_OPEN = _flag("SHIPMIND_GUARDRAIL_FAIL_OPEN", "false")

# Execution optimisation.
# Orders at or below this complexity are planned by the deterministic rules engine on the first pass
# (0 LLM calls); the LLM planner handles high complexity and any re-plan after validator feedback.
FASTPATH_MAX_COMPLEXITY = os.getenv("SHIPMIND_FASTPATH_MAX_COMPLEXITY", "medium").strip().lower()
if FASTPATH_MAX_COMPLEXITY not in {"none", "low", "medium", "high"}:
    raise ValueError("SHIPMIND_FASTPATH_MAX_COMPLEXITY must be none | low | medium | high")
# Route obvious new-order messages straight to the pipeline without a concierge LLM call.
FAST_ROUTE = _flag("SHIPMIND_FAST_ROUTE", "true")
# Carrier rate quotes are reusable for a short time (heal-loop re-auctions, bursts of similar orders).
QUOTE_CACHE_TTL_S = _num("SHIPMIND_QUOTE_CACHE_TTL_S", "30", float, lo=0)

# Token economics. Hard ceiling on LLM tokens per order; past BUDGET_DOWNGRADE_AT the planner is moved
# off the deep model, past 100% further model calls are refused and the order escalates.
TOKEN_BUDGET_PER_ORDER = _num("SHIPMIND_TOKEN_BUDGET_PER_ORDER", "60000", lo=1000)
BUDGET_DOWNGRADE_AT = _num("SHIPMIND_BUDGET_DOWNGRADE_AT", "0.6", float, lo=0.1, hi=1)
# USD per 1M tokens (input, output) by tier. Illustrative defaults: set your contract prices via
# SHIPMIND_MODEL_PRICES='{"fast": [0.3, 2.5], "balanced": [0.5, 3.0], "deep": [2.0, 12.0]}'.
MODEL_PRICES = {"fast": [0.30, 2.50], "balanced": [0.50, 3.00], "deep": [2.00, 12.00],
                **json.loads(os.getenv("SHIPMIND_MODEL_PRICES", "{}"))}
CACHED_INPUT_DISCOUNT = _num("SHIPMIND_CACHED_INPUT_DISCOUNT", "0.25", float, lo=0, hi=1)

STATE_DIR = Path(os.getenv("SHIPMIND_STATE_DIR", str(ROOT / ".shipmind")))


def route_for_complexity(complexity: str) -> str:
    """Cheap/fast model for routine orders; the deep reasoning model only when it pays off."""
    return "deep" if complexity == "high" else "balanced"


COMPLEXITY_RANK = {"none": -1, "low": 0, "medium": 1, "high": 2}


def fast_path_allowed(complexity: str) -> bool:
    return COMPLEXITY_RANK.get(complexity, 2) <= COMPLEXITY_RANK[FASTPATH_MAX_COMPLEXITY]


def price_for_model(model_id: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for a concrete model id, via its tier."""
    name = (model_id or "").removeprefix("mock:").removeprefix("models/")
    tier = next((t for t, m in MODELS.items() if name == m or name.startswith(m)), "fast")
    return tuple(MODEL_PRICES[tier])
