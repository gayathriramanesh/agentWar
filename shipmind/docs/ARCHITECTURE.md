# ShipMind Architecture

## 1. Layers

| Layer | Components | Responsibility |
|---|---|---|
| **Conversation** | `shipmind_concierge` | Chat, answers tracking/inventory questions via MCP, hands new orders to the pipeline |
| **Orchestration** | `fulfillment_pipeline` (Sequential) → `parallel_recon` (Parallel) → `plan_validate_heal_loop` (Loop) → reporter | Deterministic control flow; LLMs decide *within* steps, not *which* steps run |
| **Reasoning** | 7 LlmAgents across 3 model tiers + validator | Extraction, planning, critique, reporting |
| **Tools** | ADK function tools + MCP toolset | Bridge between agents and systems; keep state |
| **Engine** | `core/logistics.py` | Allocation, carrier auction, scoring, validation, dispatch. Framework-free and unit-tested |
| **Integration** | MCP server (stdio), carrier REST APIs | Warehouse/inventory/tracking systems and carriers |
| **Resilience** | `core/healing.py`, `FallbackModel`, LoopAgent | Retry, circuit breaker, fail-over, re-planning, model fallback |

Design choice: **workflow agents for control, LLM agents for judgment.** Sequential, parallel and loop structure is fixed in code, so latency is predictable and the system can't wander. LLMs only fill in the parts that need language or reasoning.

## 2. Agent graph

| Agent | Type | Model tier | Tools | Writes state |
|---|---|---|---|---|
| shipmind_concierge | LlmAgent | balanced (fast-routed for obvious orders) | MCP: track_shipment, check_inventory, get_disruptions, list_warehouses | – |
| intake_agent | LlmAgent (parallel) | fast | save_order | order, complexity, order_summary, order_rejection |
| input_guardrail_agent | LlmAgent (parallel), `output_schema=GuardrailVerdict` | fast | – | guardrail_verdict |
| inventory_agent | BaseAgent (code, parallel) | – | – | allocation, inventory_report |
| risk_agent | BaseAgent (code, parallel) | – | – | risk_report |
| planner_agent | LlmAgent (loop) | rules fast path, or **routed** by complexity + budget | reallocate_inventory, run_carrier_auction, submit_plan | allocation, auctions, plan, plan_summary |
| validator_agent | BaseAgent (code, loop) | – | `core.logistics.review_plan` | validation, feedback, dispatch, outcome, heal_iteration |
| reporter_agent | LlmAgent | fast | notify_customer (claim-checked) | customer_notification, final_report |

Gates: `start_order` (pipeline: reset per-order state + input screen), `require_valid_order` (execution: order valid
**and** guardrail `allow`, fail closed), `fast_path_plan` (planner: rules engine for low/medium complexity).
Per-order keys: `tools.ORDER_STATE`. Token accounting keys: `tokens_<agent>` (scoped by invocation).

## 3. Control loop

```
iteration k:
  planner:   k=1 and complexity <= FASTPATH_MAX_COMPLEXITY -> rules engine (0 LLM calls)
             else LLM: apply validator actions (reallocate / use_carrier) -> auctions -> submit_plan
  validator: review_plan (code)
             ├─ valid            → reserve stock + book (fail-over) → DISPATCHED / PARTIAL → escalate ✓
             ├─ healable issues  → structured issues {code, leg, action} → iteration k+1
             ├─ same issues as k-1, or k = max → ESCALATED (no wasted iterations)
             └─ not healable     → ESCALATED on the first pass
max_iterations = SHIPMIND_MAX_HEAL_ITERATIONS (default 3)
```

Issue codes and actions: `sla_breach` / `carrier_unavailable` → `use_carrier`; `sla_breach_origin`, `stock`,
`quantity_mismatch`, `duplicate_origin`, `bad_quantity`, `unknown_origin`, `empty_plan` → `reallocate`;
`stale_quote`, `dispatch_failed` → `rerun_auction`; `insufficient_stock`, `sla_infeasible` → `escalate`.

## 4. Complexity score → model route

| Signal | Points |
|---|---|
| express SLA | +1 |
| split shipment | +2 |
| insufficient network stock | +2 |
| disrupted origin | +1 |
| heavy freight (>30 kg) | +1 |

`≥3 = high` → deep model chain; otherwise balanced chain (or the rules fast path on the first pass). It's computed deterministically in `save_order`, so routing costs zero LLM calls. A `before_model_callback` sets a context variable that `ComplexityRoutedModel` reads to pick its delegate.

## 5. Carrier scoring
For each available quote, with weights per priority (express favours ETA, economy favours price):

`score = w_price·norm(price) + w_eta·norm(eta) + w_risk·(1−reliability)/0.1 + (SLA miss ? 1 : 0)`

`best` is the lowest-scoring SLA-compliant quote; `cheapest` is kept for comparison. Loads over a carrier's parcel limit are quoted as multi-piece (up to 4 parcels).

## 6. Latency and token budget (live Gemini, routine order)

| Step | LLM calls | Notes |
|---|---|---|
| concierge | 0 | fast route for obvious new orders (1 for questions / ambiguity) |
| intake ∥ guardrail | 1 + 1 | parallel; `save_order` ends the turn (`skip_summarization`) |
| recon | 0 | deterministic agents |
| planner | 0 | rules fast path (2–3 when re-planning or complexity = high) |
| validator | 0 | deterministic `review_plan` |
| reporter | 1 | `notify_customer` ends the turn; ops report built by code |

**3 LLM calls, 2 sequential model round-trips** (was ~16 calls). Carrier I/O adds ~40–120 ms per auction
(all carriers concurrently, async tool, deadline `SHIPMIND_AUCTION_TIMEOUT_MS`, quotes cached for
`SHIPMIND_QUOTE_CACHE_TTL_S`). Booking runs legs in parallel. The MCP subprocess (concierge only) starts once.
Every LLM call is accounted (`economics.py`) against `SHIPMIND_TOKEN_BUDGET_PER_ORDER`.

## 7. Scaling to Amazon volume
- Stateless agent workers behind a Pub/Sub (or Kafka) order queue; one ADK session per order.
- `DatabaseSessionService` (or Vertex AI sessions) instead of in-memory.
- MCP server over streamable HTTP as a shared service; carrier clients with connection pooling.
- Stock is reserved atomically at booking (no overselling); move inventory into the shared store so the MCP server sees bookings.
- Route ~90% of orders (low complexity) through a rules fast-path and send only exceptions to the full agent loop. The simulator shows the engine alone handles 100+ orders/s per process.
