"""Multi-model layer.

1. model_for(role): every agent gets a FallbackModel chain, so a 429/5xx on one model is
   answered by the next one (model-level self-healing). LiteLLM ids ("openai/...") are supported
   for any tier (cross-vendor models).
2. ComplexityRoutedModel + route_by_complexity: the planner picks its chain per request:
   deep reasoning model only for high-complexity orders, fast model otherwise (latency + cost).
"""
from __future__ import annotations

import contextvars
from typing import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from core.config import FALLBACK_CHAIN, MOCK_LLM, MODELS, route_for_complexity

_route: contextvars.ContextVar[str] = contextvars.ContextVar("shipmind_route", default="balanced")


def _resolve(model_id: str):
    if "/" in model_id and not model_id.startswith(("gemini", "models/", "projects/")):
        from google.adk.models.lite_llm import LiteLlm  # requires: pip install litellm

        return LiteLlm(model=model_id)
    return model_id


def model_for(role: str):
    """Model (with automatic fallback chain) for an agent role: fast | balanced | deep | validator."""
    chain = list(dict.fromkeys(MODELS[r] for r in FALLBACK_CHAIN[role]))  # de-dupe, keep order
    if MOCK_LLM:
        from .mock_llm import ScriptedLlm

        resolved = [ScriptedLlm(model=f"mock:{m}", can_fail=i < len(chain) - 1) for i, m in enumerate(chain)]
    else:
        resolved = [_resolve(m) for m in chain]
    if len(resolved) == 1:
        return resolved[0]
    try:
        from google.adk.models import FallbackModel  # ADK >= 2.x

        return FallbackModel(models=resolved)
    except ImportError:  # older ADK: no built-in fallback, use the primary model
        return resolved[0]


class ComplexityRoutedModel(BaseLlm):
    """Delegates to a per-route model chosen by route_by_complexity() just before the call."""

    routes: dict[str, BaseLlm | str]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        target = self.routes.get(_route.get()) or self.routes["balanced"]
        if isinstance(target, str):
            from google.adk.models.registry import LLMRegistry

            target = LLMRegistry.new_llm(target)
        llm_request.model = target.model
        async for resp in target.generate_content_async(llm_request, stream=stream):
            yield resp


def routed_planner_model() -> ComplexityRoutedModel:
    return ComplexityRoutedModel(model="shipmind-complexity-router",
                                 routes={"deep": model_for("deep"), "balanced": model_for("balanced")})


def route_by_complexity(callback_context, llm_request):
    """before_model_callback: choose the planner's model tier from the order's complexity.

    Token economics: once an order has spent BUDGET_DOWNGRADE_AT of its budget, the deep tier is dropped.
    """
    from .economics import over_downgrade_threshold

    route = route_for_complexity(callback_context.state.get("complexity", "medium"))
    if route == "deep" and over_downgrade_threshold(callback_context):
        route = "balanced"
        callback_context.state["budget_downgrade"] = True
    _route.set(route)
    callback_context.state["planner_model_route"] = f"{route} ({MODELS[route]})"
    return None
