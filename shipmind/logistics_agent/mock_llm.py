"""ScriptedLlm: a deterministic stand-in for Gemini so the REAL ADK runtime runs with no API key.

Everything else is real: agent transfer, ParallelAgent, LoopAgent escalation, state templating,
MCP subprocess tool calls, carrier APIs, retries, fail-over. Only the "reasoning" is scripted.
It identifies which agent it is serving from the system instruction and acts like that agent would.
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

from core import config, data
from core.carrier_apis import Chaos

_ROLES = [("ShipMind Concierge", "concierge"), ("Intake agent", "intake"), ("Inventory agent", "inventory"),
          ("Risk agent", "risk"), ("Fulfillment Planner", "planner"), ("Validator & Executor", "validator"),
          ("Reporter", "reporter")]


def _text(content) -> str:
    return " ".join(p.text for p in (content.parts or []) if getattr(p, "text", None))


def _call(name: str, **args) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _json_after(tag: str, text: str):
    i = text.find(tag)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[text.index("{", i):])[0]
    except (ValueError, json.JSONDecodeError):
        return None


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
        yield LlmResponse(content=types.Content(role="model", parts=parts),
                          usage_metadata=types.GenerateContentResponseUsageMetadata(
                              prompt_token_count=0, candidates_token_count=0, total_token_count=0))

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
                return [types.Part(text=f"Order invalid: {o}")]
            return [types.Part(text=f"{o['order_id']}: {o['sku']} x{o['quantity']} -> {o['destination_city']}, "
                                    f"{o['priority']}, complexity={o['complexity']}\nORDER_JSON {json.dumps(o)}")]
        user = " ".join(_text(c) for c in contents if c.role == "user" and not _text(c).startswith("For context"))
        sku = next((s for s in data.INVENTORY if s.split("-")[1].lower() in user.lower()), "SKU-ECHO")
        city = next((c for c in data.CITIES if c.lower() in user.lower()), "Seattle")
        prio = next((p for p in data.PRIORITY if p in user.lower()), "standard")
        qty = re.search(r"\b(\d+)\s*(?:x|units?|pcs|of)?\s*(?:\w+\s){0,2}?(?:echo|kindle|laptop|tv)", user, re.I)
        oid = re.search(r"ORD-\d+", user)
        return [_call("save_order", order_id=oid.group(0) if oid else "ORD-1000", sku=sku,
                      quantity=int(qty.group(1)) if qty else 1, destination_city=city, priority=prio)]

    def _inventory(self, si, contents, resp):
        if resp:
            a = next(iter(resp.values()))
            return [types.Part(text=f"Allocation status={a.get('status')}: " + ", ".join(
                f"{x['warehouse']} x{x['quantity']} ({x['distance_km']} km)" for x in a.get("allocations", []))
                + f"\nALLOC_JSON {json.dumps(a)}")]
        o = _json_after("ORDER_JSON", si) or {}
        return [_call("allocate_inventory", sku=o.get("sku"), quantity=o.get("quantity"),
                      destination_city=o.get("destination_city"), avoid_disrupted=False)]

    def _risk(self, si, contents, resp):
        if resp:
            d = next(iter(resp.values())).get("disruptions", {})
            return [types.Part(text="Active disruptions: " + "; ".join(
                f"{k} {v['type']} (+{v['delay_days']}d, {v['severity']})" for k, v in d.items()))]
        return [_call("get_disruptions")]

    def _planner(self, si, contents, resp):
        feedback = si.split("previous iteration (empty on the first pass):", 1)[-1].split("Steps:", 1)[0]
        alloc = None
        for c in reversed(contents):  # latest re-allocation made by this planner, if any
            for p in c.parts or []:
                if p.function_response and p.function_response.name == "allocate_inventory":
                    alloc = _payload(p.function_response)
                    break
            if alloc:
                break
        alloc = alloc or _json_after("ALLOC_JSON", si) or {"allocations": []}
        if "submit_plan" in resp:
            p = resp["submit_plan"]
            return [types.Part(text=f"Plan: " + ", ".join(f"{l['origin']}->{l['carrier']} x{l['quantity']}"
                                                          for l in p.get("legs", []))
                                    + f" | total ${p.get('total_cost_usd')} | max eta {p.get('max_eta_days')}d")]
        if "run_carrier_auction" in resp or any(k == "run_carrier_auction" for k in resp):
            auctions = {}
            for c in reversed(contents):
                for part in c.parts or []:
                    fr = part.function_response
                    if fr and fr.name == "run_carrier_auction":
                        a = _payload(fr)
                        auctions.setdefault(a.get("origin"), a)
            overrides = dict(re.findall(r"\[leg (\w+)\][^\n]*?use (\w+)", feedback))
            greedy = config.MOCK_GREEDY_DRAFT and "FEEDBACK" not in feedback
            legs = alloc.get("allocations", [])
            carriers = []
            for leg in legs:
                a = auctions.get(leg["warehouse"], {})
                pick = overrides.get(leg["warehouse"]) or (
                    (a.get("cheapest") if greedy else a.get("best")) or a.get("cheapest") or {}).get("carrier", "UPS")
                carriers.append(pick)
            return [_call("submit_plan", origins=[l["warehouse"] for l in legs],
                          quantities=[l["quantity"] for l in legs], carriers=carriers)]
        if "allocate_inventory" in resp or "reallocate" not in feedback:
            return [_call("run_carrier_auction", origin_warehouse=l["warehouse"], quantity=l["quantity"])
                    for l in alloc.get("allocations", [])] or [types.Part(text="Nothing to plan (no stock).")]
        o = _json_after("ORDER_JSON", si) or {}
        return [_call("allocate_inventory", sku=o.get("sku"), quantity=o.get("quantity"),
                      destination_city=o.get("destination_city"), avoid_disrupted=True)]

    def _validator(self, si, contents, resp):
        if "exit_loop" in resp:
            return [types.Part(text=("APPROVED: " if "ESCALATED" not in str(resp) else "") + resp["exit_loop"]["reason"])]
        if "dispatch_shipment" in resp:
            d = resp["dispatch_shipment"]
            if d.get("status") in ("DISPATCHED", "PARTIAL"):
                ids = ", ".join(s["tracking_id"] for s in d["shipments"])
                return [_call("exit_loop", reason=f"{d['status']} {ids}; total ${d['total_cost_usd']}, eta {d['max_eta_days']}d")]
            return [types.Part(text="FEEDBACK: dispatch failed on all carriers; re-run carrier auction.")]
        if "validate_plan" in resp:
            v = resp["validate_plan"]
            if v.get("valid"):
                return [_call("dispatch_shipment")]
            if not v.get("healable", True):
                return [_call("exit_loop", reason="ESCALATED: " + "; ".join(i["message"] for i in v["issues"]))]
            return [types.Part(text="FEEDBACK:\n" + "\n".join(
                f"[leg {i.get('leg', '-')}] {i['code']}: {i['message']} -> {i['suggestion']}" for i in v["issues"]))]
        return [_call("validate_plan")]

    def _reporter(self, si, contents, resp):
        o = _json_after("ORDER_JSON", si) or {}
        if "notify_customer" in resp:
            body = si.split("Plan:", 1)[-1].split("1. Call", 1)[0].strip()
            return [types.Part(text=f"OPS REPORT {o.get('order_id')}\n{body}")]
        outcome = si.split("Validator outcome:", 1)[-1].split("Self-healing", 1)[0].strip()
        msg = (f"Your order {o.get('order_id')} is on its way. {outcome[:160]}" if "APPROVED" in outcome
               else f"Your order {o.get('order_id')} needs attention from our team: {outcome[:160]}")
        return [_call("notify_customer", order_id=o.get("order_id", "?"), message=msg)]
