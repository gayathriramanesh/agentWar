"""Central configuration. Every knob is an env var so the team can tune without code changes."""
from __future__ import annotations

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


# Model tiers. Verify IDs in Google AI Studio's model list and override in .env if needed.
MODELS = {
    "fast": os.getenv("SHIPMIND_FAST_MODEL", "gemini-2.5-flash"),
    "balanced": os.getenv("SHIPMIND_BALANCED_MODEL", "gemini-3.5-flash"),
    "deep": os.getenv("SHIPMIND_DEEP_MODEL", "gemini-3.1-pro-preview"),
    # Any LiteLLM id (e.g. "openai/gpt-4o-mini") gives a cross-vendor second opinion (pip install litellm).
    "validator": os.getenv("SHIPMIND_VALIDATOR_MODEL", "gemini-3.5-flash"),
}

# Self-healing model chains: on 429/5xx the next model in the chain answers instead.
FALLBACK_CHAIN = {
    "fast": ["fast", "balanced"],
    "balanced": ["balanced", "fast"],
    "deep": ["deep", "balanced", "fast"],
    "validator": ["validator", "balanced"],
}

USE_MCP = _flag("SHIPMIND_USE_MCP", "true")

# Offline mode: scripted LLM drives the real ADK runtime. "auto" = on when no Gemini credentials exist.
_mock = os.getenv("SHIPMIND_MOCK_LLM", "auto").strip().lower()
HAS_GEMINI_CREDS = bool(os.getenv("GOOGLE_API_KEY")) or _flag("GOOGLE_GENAI_USE_VERTEXAI", "false")
MOCK_LLM = (not HAS_GEMINI_CREDS) if _mock == "auto" else _mock in {"1", "true", "yes", "on"}
# Mock planner drafts the cheapest plan first so the validator->heal loop is visible in demos.
MOCK_GREEDY_DRAFT = _flag("SHIPMIND_MOCK_GREEDY_DRAFT", "true")
MAX_HEAL_ITERATIONS = int(os.getenv("SHIPMIND_MAX_HEAL_ITERATIONS", "3"))
MOCK_LATENCY_MS = int(os.getenv("SHIPMIND_MOCK_LATENCY_MS", "40"))
CHAOS_RATE = float(os.getenv("SHIPMIND_CHAOS_RATE", "0"))
CHAOS_SEED = int(os.getenv("SHIPMIND_CHAOS_SEED", "42"))
STATE_DIR = Path(os.getenv("SHIPMIND_STATE_DIR", str(ROOT / ".shipmind")))


def route_for_complexity(complexity: str) -> str:
    """Cheap/fast model for routine orders; the deep reasoning model only when it pays off."""
    return "deep" if complexity == "high" else "balanced"
