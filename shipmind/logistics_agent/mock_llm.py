"""ScriptedLlm: a deterministic stand-in for Gemini so the REAL ADK runtime runs with no API key.

Everything else is real: agent transfer, ParallelAgent, LoopAgent escalation, state templating,
MCP subprocess tool calls, carrier APIs, retries, fail-over. Only the "reasoning" is scripted.
It identifies which agent it is serving from the system instruction and acts like that agent would.
Like a careful real model, intake extracts only what the user actually wrote (no defaults), so the
guardrails see the same anomalies they would in production.
Enabled with SHIPMIND_MOCK_LLM=true (or automatically when no GOOGLE_API_KEY is set).
"""
from __future__ import annotations

import json
import re
from typing import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors, types

from core import config, data, guards
from core.carrier_apis import Chaos

_ROLES = [("ShipMind Concierge", "concierge"), ("Intake agent", "intake"), ("Input Guardrail", "guardrail"),
          ("Fulfillment Planner", "planner"), ("You are the Reporter", "reporter")]


def _text(content) -> str:
    return " ".join(p.text for p in (content.parts or []) if getattr(p, "text", None))


def _call(name: str, **args) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _json_after(tag: str, text: str):
    i = text.rfind(tag + " {")  # last occurrence: the prompt prose also names the tag
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[text.index("{", i):])[0]
    except (ValueError, json.JSONDecodeError):
        return None


def _messages(si: str) -> list[str]:
    i = si.rfind("USER_MESSAGES_JSON [")  # last occurrence: the prompt prose also names the tag
    try:
        return json.JSONDecoder().raw_decode(si[i + len("USER_MESSAGES_JSON "):])[0] if i >= 0 else []
    except (ValueError, json.JSONDecodeError):
        return []


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)  # ~4 chars/token: close enough for offline cost accounting


def _payload(resp) -> dict:
    """Unwrap FunctionTool ({...}) and MCP ({'content':[{'text': json}]}) responses."""
    r = resp.response or {}
    if "content" in r and isinstance(r["content"], list) and r["content"] and "text" in r["content"][0]:
        try:
            return json.loads(r["content"][0]["text"])
        except json.JSONDecodeError:
            return {"text": r["content"][0]["text"]}
    return r.get("result", r) if isinstance(r.get("result", r), dict) else r


MODEL_HEAL_EVENTS: list[str] = []  # model fall-backs observed in this process (for the trace)
MODEL_CALLS: list[str] = []  # role of every successful model call (latency budget regression tests)


class ScriptedLlm(BaseLlm):
    model: str = "shipmind-scripted-mock"
    can_fail: bool = False  # primary models fail under chaos so ADK's FallbackModel heals the call

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        if self.can_fail:
            key = f"{len(llm_request.contents)}|{_text(llm_request.contents[-1]) if llm_request.contents else ''}"
            if Chaos.should_fail("llm", f"{self.model}|{key}"):
                MODEL_HEAL_EVENTS.append(f"{self.model} -> 429, FallbackModel moved to next model")
                raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                         "message": "quota exceeded (simulated chaos)"}})
        si = llm_request.config.system_instruction if llm_request.config else ""
        si = si if isinstance(si, str) else _text(si) if si else ""
        role = next((r for marker, r in _ROLES if marker in si), "concierge")
        last = llm_request.contents[-1] if llm_request.contents else None
        responses = [p.function_response for p in (last.parts or []) if p.function_response] if last else []
        parts = getattr(self, f"_{role}")(si, llm_request.contents, {r.name: _payload(r) for r in responses})
        MODEL_CALLS.append(role)
        # Estimated usage (system prompt + history + tool declarations in, parts out) so the token
        # economics layer is exercised offline exactly as with a real model.
        tools_decl = json.dumps([t.model_dump(exclude_none=True) for t in (llm_request.config.tools or [])],
                                default=str) if llm_request.config else ""
        history = json.dumps([c.model_dump(exclude_none=True) for c in llm_request.contents], default=str)
        n_in = _tokens(si + history + tools_decl)
        n_out = _tokens(json.dumps([p.model_dump(exclude_none=True) for p in parts], default=str))
        yield LlmResponse(content=types.Content(role="model", parts=parts), model_version=self.model,
                          usage_metadata=types.GenerateContentResponseUsageMetadata(
                              prompt_token_count=n_in, candidates_token_count=n_out, total_token_count=n_in + n_out))

    # ---------------------------------------------------------------- per-agent scripts
    def _concierge(self, si, contents, resp):
        if resp:
            return [types.Part(text="Here is what I found:\n" + json.dumps(next(iter(resp.values())), indent=1)[:800])]
        user = _text(contents[-1]) if contents else ""
        tid = re.search(r"SM-[A-Z]{3}-[0-9A-F]{8}", user)
        if tid:
            return [_call("track_shipment", tracking_id=tid.group(0))]
        if re.search(r"disruption|storm|delay", user, re.I) and not re.search(r"ship|send|deliver", user, re.I):
            return [_call("get_disruptions")]
        return [_call("transfer_to_agent", agent_name="fulfillment_pipeline")]

    def _intake(self, si, contents, resp):
        if "save_order" in resp:
            o = resp["save_order"]
            if "error" in o:
                why = o.get("ungrounded") or o.get("reasons") or o["error"]
                return [types.Part(text=f"REJECTED: {why}")]
            return [types.Part(text=f"{o['order_id']}: {o['sku']} x{o['quantity']} -> {o['destination_city']}, "
                                    f"{o['priority']}, complexity={o['complexity']}")]
        user = " ".join(_messages(si))
        low = user.lower()
        skus = sorted(guards.mentioned_skus(user))
        city = next((c for c in data.CITIES if re.search(rf"\b{c.lower()}\b", low)), None) or next(
            (c for a, c in data.CITY_ALIASES.items() if re.search(rf"\b{a}\b", low)), None)
        if not city:  # pass an unknown destination through verbatim, as a faithful model would
            m = re.search(r"\bto\s+([A-Z][\w]*(?:\s+[A-Z][\w]*)*)", user)
            city = m.group(1) if m else ""
        prio = next((p for p, words in data.PRIORITY_KEYWORDS.items()
                     if any(re.search(rf"\b{w}\b", low) for w in words)), "")
        qty = re.search(r"(?<![\w-])(-?\d+(?:\.\d+)?)\s*(?:x\s*)?(?:units?\s+|pcs\s+)?(?:of\s+)?(?:\w+\s){0,2}?"
                        r"(?:echo|kindle|laptop|tv|iphone|phone)", user, re.I)
        word = re.search(r"\b(" + "|".join(guards._NUMBER_WORDS) + r")\s+(?:\w+\s+)?(?:echo|kindle|laptop|tv)", low)
        quantity = (float(qty.group(1)) if "." in qty.group(1) else int(qty.group(1))) if qty else (
            guards._NUMBER_WORDS[word.group(1)] if word else
            1 if re.search(r"\b(?:a|an)\s+(?:\w+\s+)?(?:echo|kindle|laptop|tv)", low) else 0)
        oid = re.search(r"ORD-\d+", user)
        return [_call("save_order", order_id=oid.group(0) if oid else "", sku=skus[0] if skus else "",
                      quantity=quantity, destination_city=city, priority=prio)]

    def _guardrail(self, si, contents, resp):
        """Mimics an LLM judge: semantic red flags the regex screen and field validators don't cover."""
        text = " ".join(_messages(si))
        low, anomalies, verdict = text.lower(), [], "allow"
        if guards.screen_user_text(text) or re.search(r"\bfree\b|\$0\b|no charge|without (?:them|the "
                                                         r"recipient) knowing|someone else's order", low):
            anomalies.append("manipulation / fraud signal")
            verdict = "block"
        skus = guards.mentioned_skus(text)
        if not skus:
            anomalies.append("no catalog product mentioned")
        elif len(skus) > 1:
            anomalies.append("several products in one request")
        if not (any(re.search(rf"\b{c.lower()}\b", low) for c in data.CITIES)
                or any(re.search(rf"\b{a}\b", low) for a in data.CITY_ALIASES)):
            anomalies.append("destination outside the delivery network")
        for n in re.findall(r"(?<![\w-])(-?\d+(?:\.\d+)?)(?=x?\b)", text):
            if float(n) <= 0 or not float(n).is_integer() or float(n) > config.MAX_UNITS_PER_ORDER:
                anomalies.append(f"implausible quantity {n}")
        if re.search(r"\b(?:economy|standard)\b", low) and re.search(r"\b(?:tomorrow|today|overnight|tonight)\b", low):
            anomalies.append("slow priority but next-day deadline")
        if verdict != "block" and anomalies:
            verdict = "clarify"
        out = {"verdict": verdict, "risk": "high" if verdict == "block" else "medium" if anomalies else "low",
               "anomalies": anomalies, "reason": "; ".join(anomalies) or "single clear in-catalog order"}
        return [types.Part(text=json.dumps(out))]

    def _planner(self, si, contents, resp):
        ctx = _json_after("CONTEXT_JSON", si) or {}
        issues = ctx.get("validator_issues") or []
        if "submit_plan" in resp:
            p = resp["submit_plan"]
            if "error" in p:
                return [types.Part(text=f"Plan rejected by tool: {p}")]
            return [types.Part(text="Plan: " + ", ".join(f"{l['origin']}->{l['carrier']} x{l['quantity']}"
                                                         for l in p.get("legs", []))
                                    + f" | total ${p.get('total_cost_usd')} | max eta {p.get('max_eta_days')}d")]
        alloc = None
        for c in reversed(contents):  # this turn's re-allocation, if the planner made one
            for part in c.parts or []:
                if part.function_response and part.function_response.name == "reallocate_inventory":
                    alloc = _payload(part.function_response)
            if alloc:
                break
        alloc = alloc or ctx.get("allocation") or {"allocations": []}
        if "run_carrier_auction" in resp:
            auctions = {}
            for c in contents:
                for part in c.parts or []:
                    fr = part.function_response
                    if fr and fr.name == "run_carrier_auction":
                        a = _payload(fr)
                        auctions[a.get("origin")] = a
            overrides = {i["leg"]: i["action"]["carrier"] for i in issues
                         if i.get("leg") and (i.get("action") or {}).get("type") == "use_carrier"}
            greedy = config.MOCK_GREEDY_DRAFT and not issues
            legs = alloc.get("allocations", [])
            carriers = []
            for leg in legs:
                a = auctions.get(leg["warehouse"], {})
                pick = overrides.get(leg["warehouse"]) or (
                    (a.get("cheapest") if greedy else a.get("best")) or a.get("cheapest") or {}).get("carrier", "UPS")
                carriers.append(pick)
            return [_call("submit_plan", origins=[l["warehouse"] for l in legs],
                          quantities=[l["quantity"] for l in legs], carriers=carriers)]
        realloc = next((i["action"] for i in issues if (i.get("action") or {}).get("type") == "reallocate"), None)
        if realloc and "reallocate_inventory" not in resp:
            return [_call("reallocate_inventory", avoid_disrupted=bool(realloc.get("avoid_disrupted")))]
        return [_call("run_carrier_auction", origin_warehouse=l["warehouse"], quantity=l["quantity"])
                for l in alloc.get("allocations", [])] or [types.Part(text="Nothing to plan (no stock).")]

    def _reporter(self, si, contents, resp):
        ctx = _json_after("CONTEXT_JSON", si) or {}
        o, outcome = ctx.get("order") or {}, ctx.get("outcome") or {}
        if "notify_customer" in resp:
            legs = "; ".join(f"{x['origin']} -> {x['carrier']}, eta {x['eta_days']}d, ${x['price_usd']}"
                             for x in ctx.get("shipments", [])) or "none booked"
            return [types.Part(text=f"OPS REPORT {o.get('order_id')}\nStatus: {outcome.get('status')} | "
                                    f"{legs} | total ${ctx.get('total_cost_usd')}\n"
                                    f"Self-healing: {ctx.get('self_healing')}")]
        ids = ", ".join(x["tracking_id"] for x in ctx.get("shipments", []))
        msg = (f"Your order {o.get('order_id')} is on its way (tracking {ids})." if outcome.get("status") == "DISPATCHED"
               else f"Your order {o.get('order_id')} needs attention from our team; we'll follow up shortly."
               + (f" Already shipped: {ids}." if ids else ""))
        return [_call("notify_customer", message=msg)]
